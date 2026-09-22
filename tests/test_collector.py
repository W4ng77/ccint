"""M2 Collector 单测。[MUST] 用录制 fixture，测试中不打网络。"""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ccint.collectors.bluesky import (MAX_LIMIT, BlueskyCollector, _iso_z,
                                      _parse_created_at)
from ccint.collectors.errors import CollectorError
from ccint.config import Settings

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "searchPosts_page.json").read_text())


def _cfg(tmp_path) -> Settings:
    s = Settings()
    s.bluesky_handle = "test.bsky.social"
    s.bluesky_app_password = "aaaa-bbbb-cccc-dddd"
    s.bluesky_session_path = tmp_path / "session.json"
    return s


def _collector(handler, tmp_path, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://bsky.social")
    return BlueskyCollector(["ransomware"], query_version="cyber_v1",
                            cfg=_cfg(tmp_path), client=client,
                            sleep=lambda _s: None, **kw)


# -- 时间处理 -------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("2026-09-18T20:00:22.697Z", "2026-09-18T20:00:22.697000+00:00"),
    ("2026-09-18T20:00:22Z", "2026-09-18T20:00:22+00:00"),
    ("2026-09-18T20:00:22+00:00", "2026-09-18T20:00:22+00:00"),
    ("2026-09-18T20:00:22.1234567Z", "2026-09-18T20:00:22.123456+00:00"),
])
def test_parse_created_at_variants(raw, expect):
    assert _parse_created_at(raw).isoformat() == expect


def test_iso_z_rejects_naive():
    with pytest.raises(ValueError):
        _iso_z(datetime(2026, 9, 18))


# -- 字段映射 -------------------------------------------------------------

def test_field_mapping_and_utc(tmp_path):
    def handler(req):
        if "createSession" in str(req.url):
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        return httpx.Response(200, json=FIXTURE)

    with _collector(handler, tmp_path) as c:
        page = c.fetch_page(since=None, until=None, cursor=None, limit=100)

    assert page.posts, "fixture 应至少含一条 post"
    for p in page.posts:
        assert p.source_key == "bluesky"
        assert p.source_post_id.startswith("at://")
        assert p.author_id.startswith("did:")
        # [MUST] 时间一律 tz-aware UTC
        assert p.published_at.tzinfo is not None
        assert p.published_at.utcoffset().total_seconds() == 0
        assert isinstance(p.raw_payload, dict) and p.raw_payload
        if p.author_handle:
            assert p.url and p.url.startswith("https://bsky.app/profile/")


def test_raw_payload_preserved_verbatim(tmp_path):
    """[MUST] Raw 永不丢弃：raw_payload 必须与 API 原始对象逐字节一致。"""
    def handler(req):
        if "createSession" in str(req.url):
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        return httpx.Response(200, json=FIXTURE)

    with _collector(handler, tmp_path) as c:
        page = c.fetch_page(since=None, until=None, cursor=None, limit=100)
    assert page.posts[0].raw_payload == FIXTURE["posts"][0]


# -- 分页终止条件 ---------------------------------------------------------

def test_pagination_stops_only_on_missing_cursor(tmp_path):
    """[MUST] 实测 limit=100 会返回 99 条 —— 不得用 len(posts)<limit 判定到底。"""
    calls = {"n": 0}
    short_page = {"posts": FIXTURE["posts"][:3], "cursor": "c1"}   # 3 < limit 但未到底

    def handler(req):
        if "createSession" in str(req.url):
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=short_page)
        return httpx.Response(200, json={"posts": FIXTURE["posts"][:2]})  # 无 cursor → 到底

    with _collector(handler, tmp_path) as c:
        pages = list(c.iter_term_pages("ransomware", since=None, until=None))
    assert len(pages) == 2, "短页不应提前终止；只有 cursor 缺失才终止"
    assert pages[0].next_cursor == "c1"
    assert pages[1].next_cursor is None


def test_pagination_stops_on_empty_page(tmp_path):
    def handler(req):
        if "createSession" in str(req.url):
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        return httpx.Response(200, json={"posts": [], "cursor": "always"})

    with _collector(handler, tmp_path) as c:
        pages = list(c.iter_term_pages("ransomware", since=None, until=None, max_pages=10))
    assert len(pages) == 1, "有 cursor 但空页应停止，否则空转烧配额"


# -- rate limit / 退避 ----------------------------------------------------

def test_429_triggers_backoff_then_succeeds(tmp_path):
    seq = {"n": 0}
    slept = []

    def handler(req):
        if "createSession" in str(req.url):
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        seq["n"] += 1
        if seq["n"] <= 2:
            return httpx.Response(429, json={"error": "RateLimitExceeded"},
                                  headers={"Retry-After": "7"})
        return httpx.Response(200, json=FIXTURE)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://bsky.social")
    c = BlueskyCollector(["ransomware"], query_version="cyber_v1", cfg=_cfg(tmp_path),
                         client=client, sleep=slept.append)
    page = c.fetch_page(since=None, until=None, cursor=None, limit=100)
    assert page.posts
    assert slept == [7.0, 7.0], "必须尊重 Retry-After"
    assert c.n_http_errors == 2, "每次 HTTP 失败都要计入 n_error"
    c.close()


def test_400_not_retried(tmp_path):
    seq = {"n": 0}

    def handler(req):
        if "createSession" in str(req.url):
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        seq["n"] += 1
        return httpx.Response(400, json={"error": "InvalidRequest"})

    with _collector(handler, tmp_path) as c:
        with pytest.raises(CollectorError):
            c.fetch_page(since=None, until=None, cursor=None, limit=101)
    assert seq["n"] == 1, "400 是参数错误，重试无意义"


# -- session 复用（保护 createSession 10 次/天配额）-----------------------

def test_session_persisted_and_reused(tmp_path):
    logins = {"n": 0}

    def handler(req):
        if "createSession" in str(req.url):
            logins["n"] += 1
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        return httpx.Response(200, json=FIXTURE)

    cfg = _cfg(tmp_path)
    for _ in range(3):
        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://bsky.social")
        c = BlueskyCollector(["ransomware"], query_version="cyber_v1", cfg=cfg,
                             client=client, sleep=lambda _s: None)
        c.fetch_page(since=None, until=None, cursor=None, limit=100)
        c.close()
    assert logins["n"] == 1, "session 必须落盘复用；否则会烧光 10 次/天的配额"
    assert cfg.bluesky_session_path.exists()


def test_401_refreshes_without_new_login(tmp_path):
    logins = {"n": 0}
    refreshes = {"n": 0}
    first = {"done": False}

    def handler(req):
        u = str(req.url)
        if "createSession" in u:
            logins["n"] += 1
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        if "refreshSession" in u:
            refreshes["n"] += 1
            return httpx.Response(200, json={"accessJwt": "A2", "refreshJwt": "R2"})
        if not first["done"]:
            first["done"] = True
            return httpx.Response(401, json={"error": "ExpiredToken"})
        return httpx.Response(200, json=FIXTURE)

    with _collector(handler, tmp_path) as c:
        page = c.fetch_page(since=None, until=None, cursor=None, limit=100)
    assert page.posts
    assert refreshes["n"] == 1
    assert logins["n"] == 1, "401 应走 refreshJwt，不得消耗 createSession 配额"


# -- query_spec -----------------------------------------------------------

def test_query_spec_records_everything(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})

    with _collector(handler, tmp_path) as c:
        spec = c.query_spec()
    for k in ("source_key", "endpoint", "query_version", "search_terms", "limit"):
        assert k in spec
    assert spec["limit"] == MAX_LIMIT


# -- token 过期：实测走 HTTP 400 而非 401 ---------------------------------

def test_400_expired_token_triggers_refresh_not_failure(tmp_path):
    """回归：Bluesky 对过期 accessJwt 返回 400 + {"error":"ExpiredToken"}。

    早期实现只处理 401，把 400 一律当参数错误抛出，导致 accessJwt 过期后
    连续 10 小时零产出 —— 而 refreshJwt 当时还有两个月有效期。
    """
    logins, refreshes = {"n": 0}, {"n": 0}
    expired = {"done": False}

    def handler(req):
        u = str(req.url)
        if "createSession" in u:
            logins["n"] += 1
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        if "refreshSession" in u:
            refreshes["n"] += 1
            return httpx.Response(200, json={"accessJwt": "A2", "refreshJwt": "R2"})
        if not expired["done"]:
            expired["done"] = True
            return httpx.Response(400, json={"error": "ExpiredToken",
                                             "message": "Token has expired"})
        return httpx.Response(200, json=FIXTURE)

    with _collector(handler, tmp_path) as c:
        page = c.fetch_page(since=None, until=None, cursor=None, limit=100)
    assert page.posts, "刷新后必须继续取回数据，而不是整轮失败"
    assert refreshes["n"] == 1
    assert logins["n"] == 1, "过期应走 refreshJwt，不得消耗 createSession 的 10 次/天配额"


def test_400_real_param_error_still_not_retried(tmp_path):
    """真正的参数错误仍然立即抛出，不得被误判成 token 问题。"""
    seq = {"n": 0}

    def handler(req):
        if "createSession" in str(req.url):
            return httpx.Response(200, json={"accessJwt": "A", "refreshJwt": "R"})
        seq["n"] += 1
        return httpx.Response(400, json={"error": "InvalidRequest",
                                         "message": "limit must be <= 100"})

    with _collector(handler, tmp_path) as c:
        with pytest.raises(CollectorError, match="InvalidRequest"):
            c.fetch_page(since=None, until=None, cursor=None, limit=101)
    assert seq["n"] == 1


@pytest.mark.parametrize("code,body,expect", [
    (400, {"error": "ExpiredToken"}, True),
    (401, {"error": "AuthMissing"}, True),
    (400, {"error": "InvalidRequest"}, False),
    (403, None, False),          # 代理返回的 HTML，非 JSON
])
def test_token_error_detection(code, body, expect):
    from ccint.collectors.bluesky import _is_token_error
    r = (httpx.Response(code, json=body) if body
         else httpx.Response(code, text="<html>403 Forbidden</html>"))
    assert _is_token_error(r) is expect
