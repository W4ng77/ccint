"""M5 Trend 单测。合成数据断言排序与字段计算。"""
from datetime import datetime, timedelta, timezone

import pytest

from ccint.analytics.trend import detect_trends, window_bounds

UTC = timezone.utc
AS_OF = datetime(2026, 9, 20, tzinfo=UTC)
WINDOW = 7


def _seed(conn, rows):
    """rows: (topic, day_offset_from_as_of(负数), author, n)"""
    conn.execute("""INSERT INTO collection_runs
                    (run_id, source_key, mode, query_version, query_spec, status)
                    VALUES (1,'test','backfill','v','{}','success')
                    ON CONFLICT DO NOTHING""")
    pid = 0
    for topic, off, author, n in rows:
        for _ in range(n):
            pid += 1
            ts = AS_OF + timedelta(days=off, hours=1)
            conn.execute(
                """INSERT INTO posts (source_key, source_post_id, author_id,
                       published_at, first_seen_run, text, text_hash, raw_payload)
                   VALUES ('test', %s, %s, %s, 1, 'x', 'h', '{}')""",
                (f"p{pid}", author, ts),
            )
            conn.execute(
                """INSERT INTO post_labels (post_id, label_version, is_cyber,
                       is_canada, is_relevant, topic_key)
                   SELECT post_id,'rules_v1',true,true,true,%s FROM posts
                   WHERE source_post_id=%s""",
                (topic, f"p{pid}"),
            )


def _run(conn, **kw):
    return detect_trends(conn, label_version="rules_v1", as_of=AS_OF,
                         window_days=WINDOW, **kw)


def test_window_bounds():
    b = window_bounds(AS_OF, 7)
    assert b["cur_start"] == AS_OF - timedelta(days=7)
    assert b["prev_end"] == b["cur_start"], "两窗口必须首尾相接"
    assert b["prev_start"] == AS_OF - timedelta(days=14)


def test_tripled_topic_ranks_first(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [
            ("ransomware", -3, "a1", 30), ("ransomware", -10, "a1", 10),   # 翻三倍
            ("phishing_scam", -3, "b1", 20), ("phishing_scam", -10, "b1", 20),  # 持平
        ])
        cands = _run(conn)
    assert cands[0].topic_key == "ransomware"
    top = cands[0]
    assert top.cur_posts == 30 and top.prev_posts == 10
    assert top.abs_delta == 20
    assert top.rel_growth == pytest.approx(2.0)


def test_flat_topic_has_zero_delta(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [("phishing_scam", -3, "b1", 20), ("phishing_scam", -10, "b1", 20)])
        c = _run(conn)[0]
    assert c.abs_delta == 0
    assert c.rel_growth == pytest.approx(0.0)


def test_single_author_spam_is_visible_via_authors(clean_db):
    """[MUST] 一个人刷 100 条会造成假爆发，unique_authors 必须能立刻暴露它。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [
            ("ddos_outage", -3, "spammer", 100), ("ddos_outage", -10, "x1", 5),
            ("data_breach", -3, "d1", 10), ("data_breach", -3, "d2", 10),
            ("data_breach", -3, "d3", 10), ("data_breach", -10, "d1", 5),
        ])
        cands = {c.topic_key: c for c in _run(conn)}
    spam = cands["ddos_outage"]
    assert spam.cur_posts == 100
    assert spam.cur_authors == 1, "刷量 topic 的 author 数必须为 1"
    organic = cands["data_breach"]
    assert organic.cur_authors == 3
    # 排名仍按 abs_delta（agent 与报告负责解读 author 数），但信号必须在
    assert spam.cur_posts / spam.cur_authors == 100


def test_prev_zero_gives_none_not_inf(clean_db):
    """[MUST] prev=0 时 rel_growth 为 None，不得写成 inf，且不抛异常。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [("gov_advisory", -3, "g1", 12)])   # previous 窗口无数据
        c = _run(conn)[0]
    assert c.prev_posts == 0
    assert c.rel_growth is None
    assert c.prev_share == 0.0


def test_min_posts_filters_small_sample_noise(clean_db):
    """min_posts 挡掉 2 → 6 这类噪声。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [("fraud_financial", -3, "f1", 6), ("fraud_financial", -10, "f1", 2)])
        assert _run(conn, min_posts=5) != []
        assert _run(conn, min_posts=10) == [], "cur_posts=6 应被 min_posts=10 挡掉"


def test_shares_sum_to_one(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [
            ("ransomware", -3, "a", 30), ("phishing_scam", -3, "b", 10),
            ("ransomware", -10, "a", 5), ("phishing_scam", -10, "b", 15),
        ])
        cands = _run(conn, min_posts=1)
    assert sum(c.cur_share for c in cands) == pytest.approx(1.0)
    assert sum(c.prev_share for c in cands) == pytest.approx(1.0)
    for c in cands:
        assert c.share_delta == pytest.approx(c.cur_share - c.prev_share)


def test_posts_outside_both_windows_excluded(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [("ransomware", -3, "a", 10), ("ransomware", -30, "a", 500)])
        c = _run(conn)[0]
    assert c.cur_posts == 10 and c.prev_posts == 0, "窗口外数据不得混入"


def test_non_relevant_excluded(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [("ransomware", -3, "a", 10)])
        conn.execute("UPDATE post_labels SET is_relevant=false WHERE post_id % 2 = 0")
        c = _run(conn, min_posts=1)[0]
    assert c.cur_posts == 5


def test_other_label_version_ignored(clean_db):
    """[MUST] 不同 label_version 必须能并存而互不干扰。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [("ransomware", -3, "a", 10)])
        conn.execute("""INSERT INTO post_labels (post_id,label_version,is_cyber,
                        is_canada,is_relevant,topic_key)
                        SELECT post_id,'rules_v2',true,true,true,'phishing_scam' FROM posts""")
        c = _run(conn, min_posts=1)
    assert [x.topic_key for x in c] == ["ransomware"]


def test_as_of_must_be_tz_aware(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        with pytest.raises(ValueError):
            detect_trends(conn, label_version="rules_v1",
                          as_of=datetime(2026, 9, 20), window_days=7)


def test_deterministic_ordering(clean_db):
    """同 abs_delta 时排序必须确定，否则报告不可复现。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed(conn, [("ransomware", -3, "a", 10), ("phishing_scam", -3, "b", 10)])
        a = [c.topic_key for c in _run(conn)]
        b = [c.topic_key for c in _run(conn)]
    assert a == b
