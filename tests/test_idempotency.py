"""[MUST] HANDOFF §10：collect / label / analyze 重复执行的行为必须有断言。

collect 的幂等在 test_ingest.py::test_collection_idempotent。
本文件覆盖 label 与 analyze。
"""
from datetime import datetime, timedelta, timezone

import pytest

from ccint.analytics.trend import detect_trends
from ccint.labeling import label_counts, label_posts
from ccint.report import data_health, persist_analysis_run, render_report

UTC = timezone.utc
AS_OF = datetime(2026, 9, 20, tzinfo=UTC)


def _seed_posts(conn, texts):
    conn.execute("""INSERT INTO collection_runs
                    (run_id, source_key, mode, query_version, query_spec, status,
                     window_start, window_end)
                    VALUES (1,'test','backfill','v','{}','success',%s,%s)
                    ON CONFLICT DO NOTHING""",
                 (AS_OF - timedelta(days=14), AS_OF))
    for i, (txt, off) in enumerate(texts):
        conn.execute(
            """INSERT INTO posts (source_key, source_post_id, author_id, author_handle,
                   published_at, first_seen_run, text, lang, text_hash, raw_payload)
               VALUES ('test',%s,%s,%s,%s,1,%s,'en','h','{}')""",
            (f"at://p{i}", f"did:plc:a{i}", f"u{i}.bsky.social",
             AS_OF + timedelta(days=off, hours=1), txt),
        )


CORPUS = [
    ("The Canada Revenue Agency warns of a phishing scam targeting taxpayers.", -2),
    ("LockBit ransomware claims attack on a Toronto hospital network.", -2),
    ("Data breach at Desjardins exposed member records in Quebec.", -3),
    ("Une cyberattaque a frappé Hydro-Québec cette semaine.", -3),
    ("Ransomware hit a hospital in Berlin; no Canadian link.", -2),
    ("wow this is insane", -2),
    ("Data breach at a Vancouver clinic exposed patient records.", -9),
    ("Phishing emails impersonating Canada Post are circulating.", -9),
]


# ---------------------------------------------------------------- label

def test_label_idempotent(clean_db):
    """[MUST] `ccint label --version rules_v1` 重复执行不改变结果、不产生重复行。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed_posts(conn, CORPUS)

    first = label_posts("rules_v1", only_unlabeled=True)
    counts_1 = label_counts("rules_v1")

    # 再跑一次 --unlabeled：应当一条都不看（全部已标注）
    second = label_posts("rules_v1", only_unlabeled=True)
    assert second["n_seen"] == 0, "--unlabeled 重跑不应重复处理已标注的 post"
    assert label_counts("rules_v1") == counts_1

    # 再跑一次 --all：重标全部，行数与判定必须完全一致
    third = label_posts("rules_v1", only_unlabeled=False)
    assert third["n_seen"] == first["n_seen"]
    assert label_counts("rules_v1") == counts_1, "--all 重标不得改变任何计数"

    with clean_db.connect(autocommit=True) as conn:
        n_rows = conn.execute(
            "SELECT count(*) AS n FROM post_labels WHERE label_version='rules_v1'"
        ).fetchone()["n"]
        n_posts = conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"]
    assert n_rows == n_posts, "每个 post 每个 version 最多一行（主键约束）"


def test_label_versions_coexist(clean_db):
    """[MUST] 换方法 = 写入新 version，posts 表一个字节都不动。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed_posts(conn, CORPUS)
        before = conn.execute("SELECT md5(string_agg(text,'|' ORDER BY post_id)) AS h "
                              "FROM posts").fetchone()["h"]
    label_posts("rules_v1", only_unlabeled=True)
    with clean_db.connect(autocommit=True) as conn:
        conn.execute("""INSERT INTO post_labels (post_id,label_version,is_cyber,
                        is_canada,is_relevant,topic_key)
                        SELECT post_id,'rules_v2',true,true,true,'other' FROM posts""")
        after = conn.execute("SELECT md5(string_agg(text,'|' ORDER BY post_id)) AS h "
                             "FROM posts").fetchone()["h"]
        vs = [r["label_version"] for r in conn.execute(
            "SELECT DISTINCT label_version FROM post_labels ORDER BY 1").fetchall()]
    assert before == after, "写入新 label_version 不得触碰 posts"
    assert vs == ["rules_v1", "rules_v2"], "旧 version 必须保留以便并排比较"


# ---------------------------------------------------------------- analyze

def _analyze_once(conn, **kw):
    cands = detect_trends(conn, label_version="rules_v1", as_of=AS_OF,
                          window_days=7, min_posts=1, **kw)
    health = data_health(conn, window_start=AS_OF - timedelta(days=7), window_end=AS_OF)
    analysis_id = persist_analysis_run(
        conn, as_of=AS_OF, label_version="rules_v1", window_days=7,
        filters={"min_posts": 1}, agent_model=None, prompt_version=None,
        report_path=None, candidates=cands, agent_result=None)
    md = render_report(analysis_id=analysis_id, as_of=AS_OF, window_days=7,
                       label_version="rules_v1", query_version="cyber_v1",
                       agent_model=None, prompt_version=None, candidates=cands,
                       health=health, agent_result=None, min_posts=1)
    return analysis_id, cands, md


def test_analyze_idempotent(clean_db):
    """[MUST] analyze 重复执行产出相同的 trend 结果。

    注意：每次 analyze **应当**新增一行 analysis_runs —— 那是 provenance，不是重复数据。
    幂等性在此指「相同输入 → 相同结论」。
    """
    with clean_db.connect(autocommit=True) as conn:
        _seed_posts(conn, CORPUS)
    label_posts("rules_v1", only_unlabeled=True)

    with clean_db.connect(autocommit=True) as conn:
        id1, c1, md1 = _analyze_once(conn)
        id2, c2, md2 = _analyze_once(conn)

        assert id1 != id2, "每次分析必须留下独立的 provenance 记录"
        assert [c.to_dict() for c in c1] == [c.to_dict() for c in c2], \
            "相同输入必须产出完全相同的 trend candidates"

        # 报告正文除时间戳/analysis_id 外必须一致
        def _strip(md):
            return [ln for ln in md.splitlines()
                    if "generated_at" not in ln and "analysis_id" not in ln]
        assert _strip(md1) == _strip(md2)

        stored = conn.execute(
            "SELECT result FROM analysis_runs ORDER BY analysis_id").fetchall()
    assert stored[0]["result"]["candidates"] == stored[1]["result"]["candidates"]


def test_analyze_snapshot_persisted(clean_db):
    """[OUT OF SCOPE] 不建 trend_snapshots 表 —— 快照进 analysis_runs.result。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed_posts(conn, CORPUS)
    label_posts("rules_v1", only_unlabeled=True)
    with clean_db.connect(autocommit=True) as conn:
        _analyze_once(conn)
        row = conn.execute("""SELECT trend_method, window_days, label_version, result
                              FROM analysis_runs""").fetchone()
    assert row["trend_method"] == "window_compare_v1"
    assert row["window_days"] == 7
    assert row["label_version"] == "rules_v1"
    assert "candidates" in row["result"]


def test_analyze_on_empty_label_version_is_graceful(clean_db):
    """空结果不得抛异常（§M7 验收：空库上给出友好报错而非崩溃）。"""
    with clean_db.connect(autocommit=True) as conn:
        cands = detect_trends(conn, label_version="nonexistent_version",
                              as_of=AS_OF, window_days=7, min_posts=1)
    assert cands == []
