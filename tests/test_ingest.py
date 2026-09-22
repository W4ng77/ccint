"""M3 Ingest 单测。[MUST] 幂等性测试是必须的，不是可选的。"""
from datetime import datetime, timedelta, timezone

import pytest

from ccint.ingest import (compute_text_hash, dedup_batch, extract_urls,
                          iter_windows, normalize, normalize_text,
                          open_run, reap_stale_runs, run_collection)
from ccint.models import Page, RawPost

UTC = timezone.utc


def _post(i: int, *, text="Ransomware hit a Toronto hospital", urls=None) -> RawPost:
    return RawPost(
        source_key="test", source_post_id=f"at://post/{i}", author_id=f"did:plc:a{i % 3}",
        author_handle=f"user{i % 3}.bsky.social",
        published_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC) + timedelta(minutes=i),
        text=text, lang="en", url=f"https://bsky.app/p/{i}", in_reply_to=None,
        reply_count=0, like_count=i, repost_count=0,
        raw_payload={"uri": f"at://post/{i}", "record": {"text": text,
                     "facets": [{"features": [{"uri": u}]} for u in (urls or [])]}},
    )


class FakeCollector:
    """按 term 返回固定页。不打网络。"""
    source_key = "test"
    query_version = "cyber_v1"

    def __init__(self, pages_per_term: dict[str, list[Page]], fail_terms=()):
        self.search_terms = list(pages_per_term)
        self._pages = pages_per_term
        self._fail = set(fail_terms)
        self.n_http_errors = 0

    def query_spec(self):
        return {"source_key": self.source_key, "search_terms": self.search_terms}

    def iter_term_pages(self, term, *, since, until, max_pages=200):
        if term in self._fail:
            raise RuntimeError(f"boom on {term}")
        yield from self._pages[term]


# -- normalize ------------------------------------------------------------

def test_text_hash_ignores_case_urls_whitespace():
    a = compute_text_hash("Hello  WORLD https://a.com/x")
    b = compute_text_hash("hello world https://completely-different.example/y")
    assert a == b
    assert a != compute_text_hash("hello there")


def test_normalize_text_collapses_whitespace_and_nbsp():
    assert normalize_text("  a\n\n b c  ") == "a b c"


def test_extract_urls_from_text_and_facets():
    p = _post(1, text="see https://example.com/a now", urls=["https://full.example/expanded"])
    urls = extract_urls(p)
    assert "https://example.com/a" in urls
    assert "https://full.example/expanded" in urls


def test_normalize_produces_tz_aware_utc():
    row = normalize(_post(1))
    assert row["published_at"].tzinfo is not None
    assert row["published_at"].utcoffset().total_seconds() == 0
    assert len(row["text_hash"]) == 64


# -- 批内去重 -------------------------------------------------------------

def test_dedup_batch_same_post_from_multiple_terms():
    """同一帖会被多个 cyber term 命中（HANDOFF §3.4 结果合并去重）。"""
    batch = [_post(1), _post(2), _post(1), _post(1)]
    assert len(dedup_batch(batch)) == 2


# -- 按天分块 -------------------------------------------------------------

def test_iter_windows_daily_chunks():
    s, u = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC)
    assert len(list(iter_windows(s, u, 1))) == 3
    assert len(list(iter_windows(s, u, None))) == 1


def test_iter_windows_covers_window_exactly():
    s, u = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, 6, tzinfo=UTC)
    ws = list(iter_windows(s, u, 1))
    assert ws[0][0] == s and ws[-1][1] == u
    for (a, b), (c, _d) in zip(ws, ws[1:]):
        assert b == c, "窗口必须首尾相接，不得有空洞或重叠"


# -- 幂等性 [MUST] --------------------------------------------------------

def test_collection_idempotent(clean_db):
    pages = {"ransomware": [Page([_post(i) for i in range(5)], None)]}
    r1 = run_collection(FakeCollector(pages), mode="backfill",
                        since=datetime(2026, 9, 10, tzinfo=UTC),
                        until=datetime(2026, 9, 11, tzinfo=UTC))
    r2 = run_collection(FakeCollector(pages), mode="backfill",
                        since=datetime(2026, 9, 10, tzinfo=UTC),
                        until=datetime(2026, 9, 11, tzinfo=UTC))
    assert r1["n_inserted"] == 5
    assert r2["n_inserted"] == 0, "重复执行不得产生重复数据"
    assert r2["n_duplicate"] == r1["n_inserted"]
    with clean_db.connect(autocommit=True) as c:
        assert c.execute("SELECT count(*) n FROM posts").fetchone()["n"] == 5


def test_raw_payload_stored_verbatim(clean_db):
    """[MUST] Raw 永不丢弃。"""
    p = _post(1)
    run_collection(FakeCollector({"t": [Page([p], None)]}), mode="backfill",
                   since=datetime(2026, 9, 10, tzinfo=UTC),
                   until=datetime(2026, 9, 11, tzinfo=UTC))
    with clean_db.connect(autocommit=True) as c:
        got = c.execute("SELECT raw_payload FROM posts").fetchone()["raw_payload"]
    assert got == p.raw_payload


# -- 异常安全 [MUST] ------------------------------------------------------

def test_run_marked_failed_on_exception(clean_db):
    """[MUST] 任何异常下 collection_runs 都必须被收尾，不留永久 running。"""
    with pytest.raises(RuntimeError):
        with open_run(source_key="test", mode="backfill", query_version="v",
                      query_spec={}, window_start=None, window_end=None):
            raise RuntimeError("kaboom")
    with clean_db.connect(autocommit=True) as c:
        r = c.execute("SELECT status, error_detail FROM collection_runs").fetchone()
    assert r["status"] == "failed"
    assert "kaboom" in r["error_detail"]


def test_run_bookkeeping_survives_data_transaction_rollback(clean_db):
    """记账走独立连接：数据事务 abort 不得连带回滚 run 记录。"""
    from ccint import db
    with pytest.raises(Exception):
        with open_run(source_key="test", mode="backfill", query_version="v",
                      query_spec={}, window_start=None, window_end=None):
            with db.transaction() as conn:
                conn.execute("INSERT INTO posts (source_key) VALUES ('bad')")  # 违反 NOT NULL
    with clean_db.connect(autocommit=True) as c:
        n = c.execute("SELECT count(*) n FROM collection_runs WHERE status='failed'").fetchone()["n"]
    assert n == 1, "run 记录必须独立于数据事务持久化"


def test_partial_status_when_a_term_fails(clean_db):
    """连续失败导致本轮不完整时状态标 partial 而非 success。"""
    col = FakeCollector({"ok": [Page([_post(1)], None)], "bad": []}, fail_terms=["bad"])
    res = run_collection(col, mode="backfill",
                         since=datetime(2026, 9, 10, tzinfo=UTC),
                         until=datetime(2026, 9, 11, tzinfo=UTC))
    assert res["n_error"] >= 1
    with clean_db.connect(autocommit=True) as c:
        assert c.execute("SELECT status FROM collection_runs").fetchone()["status"] == "partial"


def test_success_status_when_clean(clean_db):
    run_collection(FakeCollector({"ok": [Page([_post(1)], None)]}), mode="backfill",
                   since=datetime(2026, 9, 10, tzinfo=UTC),
                   until=datetime(2026, 9, 11, tzinfo=UTC))
    with clean_db.connect(autocommit=True) as c:
        assert c.execute("SELECT status FROM collection_runs").fetchone()["status"] == "success"


def test_reap_stale_running_runs(clean_db):
    """SIGKILL 无法被 except 捕获，故需回收器补上。"""
    with clean_db.connect(autocommit=True) as c:
        c.execute("""INSERT INTO collection_runs
                     (source_key, mode, query_version, query_spec, status, started_at)
                     VALUES ('test','backfill','v','{}','running', now() - interval '2 days')""")
    assert reap_stale_runs(older_than_hours=1) == 1
    with clean_db.connect(autocommit=True) as c:
        r = c.execute("SELECT status, error_detail FROM collection_runs").fetchone()
    assert r["status"] == "failed" and "reaped" in r["error_detail"]


def test_reaper_leaves_fresh_running_alone(clean_db):
    with clean_db.connect(autocommit=True) as c:
        c.execute("""INSERT INTO collection_runs
                     (source_key, mode, query_version, query_spec, status)
                     VALUES ('test','backfill','v','{}','running')""")
    assert reap_stale_runs(older_than_hours=6) == 0


# -- checkpoint -----------------------------------------------------------

def test_incremental_checkpoint_from_last_success(clean_db):
    from ccint.config import settings
    from ccint.ingest import resolve_incremental_window
    end = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    with clean_db.connect(autocommit=True) as c:
        c.execute("""INSERT INTO collection_runs
                     (source_key, mode, query_version, query_spec, status, window_end)
                     VALUES ('test','incremental','v','{}','success', %s)""", (end,))
        c.execute("""INSERT INTO collection_runs
                     (source_key, mode, query_version, query_spec, status, window_end)
                     VALUES ('test','incremental','v','{}','failed', %s)""",
                  (end + timedelta(days=5),))
    start, _ = resolve_incremental_window("test")
    expect = end - timedelta(minutes=settings.incremental_lookback_minutes)
    assert start == expect, "必须从最近一次 success 继续，忽略 failed"


def test_incremental_falls_back_without_history(clean_db):
    from ccint.ingest import resolve_incremental_window
    now = datetime(2026, 9, 20, tzinfo=UTC)
    start, until = resolve_incremental_window("nosuchsource", now=now)
    assert start < until == now


# -- NUL 字节（实战中打挂过整轮采集）--------------------------------------

def test_nul_bytes_stripped_from_text_and_payload():
    """PostgreSQL 的 text/jsonb 无法存储 U+0000，而 Bluesky 正文里确实出现过。"""
    from ccint.ingest import sanitize_payload, strip_nul
    assert strip_nul("AI\x00 talk") == "AI talk"
    assert sanitize_payload({"a\x00": ["x\x00", {"b": "c\x00"}]}) == {"a": ["x", {"b": "c"}]}
    assert "\x00" not in normalize_text("AI\x00 breach")


def test_post_with_nul_byte_is_storable(clean_db):
    p = RawPost(
        source_key="test", source_post_id="at://nul/1", author_id="did:plc:x",
        author_handle="u\x00ser", published_at=datetime(2026, 9, 10, tzinfo=UTC),
        text="AI\x00 talk about a breach", lang="en", url=None, in_reply_to=None,
        reply_count=0, like_count=0, repost_count=0,
        raw_payload={"record": {"text": "AI\x00 talk"}, "nested": ["a\x00"]},
    )
    res = run_collection(FakeCollector({"t": [Page([p], None)]}), mode="backfill",
                         since=datetime(2026, 9, 10, tzinfo=UTC),
                         until=datetime(2026, 9, 11, tzinfo=UTC))
    assert res["n_inserted"] == 1, "含 NUL 的帖子必须能入库，不得打挂整轮采集"
    with clean_db.connect(autocommit=True) as c:
        row = c.execute("SELECT text, author_handle, raw_payload FROM posts").fetchone()
    assert "\x00" not in row["text"]
    assert "\x00" not in str(row["raw_payload"])


# -- NUL 字符（PostgreSQL 无法存储 U+0000）--------------------------------

def test_strip_nul_recursive():
    from ccint.ingest import strip_nul
    assert strip_nul("AI\x00 talk") == "AI talk"
    assert strip_nul({"a": "x\x00y", "b": ["p\x00", {"c": "q\x00"}]}) == \
        {"a": "xy", "b": ["p", {"c": "q"}]}
    assert strip_nul(None) is None
    assert strip_nul(42) == 42


def test_normalize_strips_nul_everywhere():
    p = RawPost("bluesky", "at://x", "did:1", "h\x00",
                datetime(2026, 9, 10, tzinfo=UTC), "AI\x00 breach", "en",
                "https://a\x00", None, 0, 0, 0, {"record": {"text": "AI\x00"}})
    row = normalize(p)
    assert all("\x00" not in str(v) for v in row.values())


def test_post_with_nul_is_insertable(clean_db):
    """回归：run 4 曾因语料中的 U+0000 整轮失败（UntranslatableCharacter）。"""
    p = RawPost("test", "at://nul", "did:1", None,
                datetime(2026, 9, 10, tzinfo=UTC), "AI\x00 breach in Toronto", "en",
                None, None, 0, 0, 0, {"record": {"text": "AI\x00 breach"}})
    res = run_collection(FakeCollector({"t": [Page([p], None)]}), mode="backfill",
                         since=datetime(2026, 9, 10, tzinfo=UTC),
                         until=datetime(2026, 9, 11, tzinfo=UTC))
    assert res["n_inserted"] == 1
    with clean_db.connect(autocommit=True) as c:
        assert c.execute("SELECT status FROM collection_runs").fetchone()["status"] == "success"


# -- partial 的语义：不完整 ≠ 出现过错误 ----------------------------------

def test_recovered_http_error_does_not_mark_partial(clean_db):
    """accessJwt 每 ~2 小时过期，刷新前那次请求必然失败并计入 n_error。

    若据此标 partial，则每轮都是 partial，HANDOFF §4 想用 run 状态区分
    「系统故障 vs 社会趋势」的能力就被噪声淹没。
    """
    col = FakeCollector({"ok": [Page([_post(1)], None)]})
    col.n_http_errors = 3          # 重试后成功的瞬时失败
    res = run_collection(col, mode="incremental",
                         since=datetime(2026, 9, 10, tzinfo=UTC),
                         until=datetime(2026, 9, 11, tzinfo=UTC))
    assert res["n_error"] == 3, "[MUST] 所有 HTTP 失败仍须计入 n_error"
    assert res["n_incomplete"] == 0
    with clean_db.connect(autocommit=True) as c:
        assert c.execute("SELECT status FROM collection_runs").fetchone()["status"] == "success"


def test_term_that_never_completes_marks_partial(clean_db):
    """真正的不完整（某个 term 一条都没拿到）仍须标 partial。"""
    col = FakeCollector({"ok": [Page([_post(1)], None)], "bad": []}, fail_terms=["bad"])
    res = run_collection(col, mode="incremental",
                         since=datetime(2026, 9, 10, tzinfo=UTC),
                         until=datetime(2026, 9, 11, tzinfo=UTC))
    assert res["n_incomplete"] == 1
    with clean_db.connect(autocommit=True) as c:
        assert c.execute("SELECT status FROM collection_runs").fetchone()["status"] == "partial"
