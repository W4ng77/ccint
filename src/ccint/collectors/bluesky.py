"""Bluesky collector。

[决策记录 / API 实测结论] 见 README「API 实测」一节。要点：
  * 走 https://bsky.social（PDS host）。实测 *.bsky.app 在本机被出网策略拦截
    （返回 HTML 403「Request forbidden by administrative rules.」，
    而非 XRPC 的 JSON error），bsky.social 功能等价。
  * searchPosts 必须鉴权（未鉴权 → 401 AuthMissing）。
  * limit 上限 100（101 → 400 InvalidRequest）。
  * [MUST] limit=100 实测返回 99 条（被删/被过滤的帖子会让页面不满），
    因此「返回数 < limit 即到底」是错误的终止条件 —— 只能以 cursor 缺失为准。
  * since/until：date-only / ISO-Z / 带毫秒 / naive 四种格式结果一致，均按 UTC 解释。
    本实现一律发送显式 ...Z。
  * 结果按时间倒序，cursor 向历史方向翻页；实测可回溯 3 个月以上。
  * rate limit：searchPosts 3000/300s（宽松）；
    [MUST] createSession 仅 10/86400s —— 故 session 必须落盘复用 + refreshJwt 续期。
    若每轮采集重新登录，5 分钟一轮会在 50 分钟内锁死账号一整天，
    而 HANDOFF §8 要求采集永不停机。

选择直接打 HTTP（httpx）而非官方 atproto SDK：需要读取裸 RateLimit-Reset /
Retry-After header 做精确退避，SDK 会将其封装掉；且 v0 只用两个端点。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import Settings, settings as default_settings
from ..models import Page, RawPost
from .errors import AuthError, CollectorError, RateLimited

log = logging.getLogger(__name__)

SOURCE_KEY = "bluesky"
MAX_LIMIT = 100          # 实测上限；101 → 400 InvalidRequest
_MAX_ATTEMPTS = 5
_BASE_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


def _iso_z(dt: datetime) -> str:
    """tz-aware datetime → 显式 UTC 的 ISO8601。禁止 naive。"""
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# XRPC 的鉴权失效错误码。实测 ExpiredToken 走的是 HTTP 400 而非 401。
_TOKEN_ERRORS = {"ExpiredToken", "InvalidToken", "AuthMissing", "AuthenticationRequired"}


def _is_token_error(r: httpx.Response) -> bool:
    try:
        return (r.json() or {}).get("error") in _TOKEN_ERRORS
    except Exception:  # noqa: BLE001 — 非 JSON 响应（如代理返回的 HTML）
        return False


def _parse_created_at(s: str) -> datetime:
    """Bluesky 的 createdAt 格式不统一（有无毫秒、Z 或 +00:00 都出现过）。"""
    if not s:
        raise ValueError("empty createdAt")
    raw = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # 少数客户端写出超过 6 位小数秒
        if "." in raw:
            head, _, tail = raw.partition(".")
            frac = "".join(c for c in tail if c.isdigit())[:6]
            off = tail[len(frac):] if len(tail) > len(frac) else "+00:00"
            if not off.startswith(("+", "-")):
                off = "+00:00"
            dt = datetime.fromisoformat(f"{head}.{frac or '0'}{off}")
        else:
            raise
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Session：落盘复用 + refreshJwt 续期。保护 createSession 的 10 次/天配额。
# --------------------------------------------------------------------------


class BlueskySession:
    def __init__(self, cfg: Settings, client: httpx.Client):
        self.cfg = cfg
        self.client = client
        self.path = Path(cfg.bluesky_session_path)
        self._data: dict | None = None
        self.n_create_session = 0   # 本进程内的真实登录次数，用于告警

    # -- 持久化 ------------------------------------------------------------
    def _load(self) -> dict | None:
        if self._data is not None:
            return self._data
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                log.debug("session loaded from %s", self.path)
                return self._data
            except Exception as e:  # noqa: BLE001
                log.warning("session file unreadable (%s); will re-login", e)
        return None

    def _save(self, data: dict) -> None:
        self._data = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # -- 取 token ----------------------------------------------------------
    def access_token(self) -> str:
        d = self._load()
        if d and d.get("accessJwt"):
            return d["accessJwt"]
        return self._create()["accessJwt"]

    def refresh(self) -> str:
        """accessJwt 过期时调用。refreshJwt 有效期约 2 个月，不消耗 createSession 配额。"""
        d = self._load()
        if d and d.get("refreshJwt"):
            try:
                r = self.client.post(
                    "/xrpc/com.atproto.server.refreshSession",
                    headers={"Authorization": f"Bearer {d['refreshJwt']}"},
                )
                if r.status_code == 200:
                    new = r.json()
                    merged = {**d, **new}
                    self._save(merged)
                    log.info("session refreshed (no createSession quota consumed)")
                    return merged["accessJwt"]
                log.warning("refreshSession failed %s: %s", r.status_code, r.text[:160])
            except httpx.HTTPError as e:
                log.warning("refreshSession transport error: %s", e)
        return self._create()["accessJwt"]

    def _create(self) -> dict:
        if not self.cfg.bluesky_handle or not self.cfg.bluesky_app_password:
            raise AuthError(
                "CCINT_BLUESKY_HANDLE / CCINT_BLUESKY_APP_PASSWORD 未配置（见 .env.example）"
            )
        r = self.client.post(
            "/xrpc/com.atproto.server.createSession",
            json={
                "identifier": self.cfg.bluesky_handle,
                "password": self.cfg.bluesky_app_password,
            },
        )
        self.n_create_session += 1
        remaining = r.headers.get("RateLimit-Remaining")
        if r.status_code != 200:
            body = r.text[:200]
            raise AuthError(f"createSession {r.status_code}: {body}")
        data = r.json()
        self._save(data)
        log.warning(
            "createSession consumed (daily quota 10). remaining=%s policy=%s",
            remaining, r.headers.get("RateLimit-Policy"),
        )
        return data


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------


class BlueskyCollector:
    """[MUST] 只负责把东西原样拿回来：不过滤、不清洗、不做 Canada 判断。"""

    source_key = SOURCE_KEY

    def __init__(
        self,
        search_terms: list[str],
        *,
        query_version: str,
        cfg: Settings | None = None,
        client: httpx.Client | None = None,
        lang: str | None = None,
        sleep=time.sleep,
    ):
        self.cfg = cfg or default_settings
        self.query_version = query_version
        self.search_terms = list(search_terms)
        self.lang = lang
        self._sleep = sleep
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=self.cfg.bluesky_base_url,
            timeout=30.0,
            headers={"User-Agent": "ccint/0.1 (research; Canada cyber social intelligence)"},
        )
        self.session = BlueskySession(self.cfg, self.client)
        self.n_requests = 0
        self.n_http_errors = 0

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- Protocol ---------------------------------------------------------
    def query_spec(self) -> dict:
        """[MUST] 完整序列化查询条件，写入 collection_runs.query_spec。"""
        return {
            "source_key": self.source_key,
            "endpoint": "app.bsky.feed.searchPosts",
            "base_url": self.cfg.bluesky_base_url,
            "query_version": self.query_version,
            "search_terms": self.search_terms,
            "lang": self.lang,
            "limit": MAX_LIMIT,
        }

    def fetch_page(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        cursor: str | None,
        limit: int,
        term: str | None = None,
    ) -> Page:
        """单个 term 的一页。term 为 None 时取词表首个（Protocol 兼容）。"""
        q = term if term is not None else self.search_terms[0]
        params: dict[str, str | int] = {"q": q, "limit": min(limit, MAX_LIMIT)}
        if since:
            params["since"] = _iso_z(since)
        if until:
            params["until"] = _iso_z(until)
        if cursor:
            params["cursor"] = cursor
        if self.lang:
            params["lang"] = self.lang

        data = self._get("/xrpc/app.bsky.feed.searchPosts", params)
        posts = []
        for raw in data.get("posts", []):
            try:
                posts.append(self._to_raw_post(raw))
            except Exception as e:  # noqa: BLE001
                # 单条畸形不应中断整页；raw_payload 已保底，可事后重放
                log.warning("skip malformed post %s: %s", raw.get("uri"), e)
        # [MUST] 终止条件只看 cursor，不看 len(posts) < limit
        return Page(posts=posts, next_cursor=data.get("cursor"))

    def iter_term_pages(
        self,
        term: str,
        *,
        since: datetime | None,
        until: datetime | None,
        max_pages: int = 200,
    ):
        """翻完一个 term 的所有页。倒序：从 until 向历史走。"""
        cursor: str | None = None
        for page_no in range(max_pages):
            page = self.fetch_page(
                since=since, until=until, cursor=cursor, limit=MAX_LIMIT, term=term
            )
            yield page
            cursor = page.next_cursor
            if not cursor:
                log.debug("term=%r exhausted after %d page(s)", term, page_no + 1)
                return
            if not page.posts:
                # 有 cursor 但空页：继续翻会浪费配额，视为到底
                log.debug("term=%r empty page with cursor; stopping", term)
                return
        log.warning("term=%r hit max_pages=%d; may be truncated", term, max_pages)

    # -- HTTP -------------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        """[MUST] 指数退避重试 + rate-limit 处理。任何 HTTP 失败计入 n_http_errors。"""
        token = self.session.access_token()
        last_err: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                r = self.client.get(
                    path, params=params, headers={"Authorization": f"Bearer {token}"}
                )
                self.n_requests += 1
            except httpx.HTTPError as e:
                self.n_http_errors += 1
                last_err = CollectorError(f"transport error: {e}")
                self._backoff(attempt, None)
                continue

            if r.status_code == 200:
                return r.json()

            self.n_http_errors += 1

            # [MUST] token 过期的判定必须看 body 的 error 码，不能只看状态码。
            # 实测：Bluesky 对过期的 accessJwt 返回 **400 InvalidRequest** +
            # {"error":"ExpiredToken"}，而不是 401。早期实现只处理 401，导致
            # accessJwt 过期后每轮采集全量失败、连续 10 小时零产出 —— 而
            # refreshJwt 当时还有两个月有效期，修复手段一直可用却从未被调用。
            if r.status_code in (400, 401) and _is_token_error(r):
                log.info("token expired on %s (%d); refreshing session",
                         path, r.status_code)
                token = self.session.refresh()
                last_err = AuthError(f"{r.status_code}: {r.text[:160]}")
                continue

            if r.status_code == 401:
                log.info("401 on %s; refreshing session", path)
                token = self.session.refresh()
                last_err = AuthError(f"401: {r.text[:160]}")
                continue

            if r.status_code == 429:
                wait = self._rate_limit_wait(r)
                log.warning("429 rate limited; sleeping %.1fs", wait)
                last_err = RateLimited(r.text[:160], wait)
                self._sleep(wait)
                continue

            if r.status_code == 400:
                # 真正的参数错误，重试无意义（token 过期已在上面分流）
                raise CollectorError(f"400 InvalidRequest on {path}: {r.text[:200]}")

            if 500 <= r.status_code < 600:
                last_err = CollectorError(f"{r.status_code}: {r.text[:160]}")
                self._backoff(attempt, r)
                continue

            raise CollectorError(f"unexpected {r.status_code} on {path}: {r.text[:200]}")

        raise last_err or CollectorError(f"exhausted {_MAX_ATTEMPTS} attempts on {path}")

    @staticmethod
    def _rate_limit_wait(r: httpx.Response) -> float:
        ra = r.headers.get("Retry-After")
        if ra:
            try:
                return max(1.0, float(ra))
            except ValueError:
                pass
        reset = r.headers.get("RateLimit-Reset")
        if reset:
            try:
                return max(1.0, min(_MAX_BACKOFF * 5, float(reset) - time.time()))
            except ValueError:
                pass
        return 30.0

    def _backoff(self, attempt: int, r: httpx.Response | None) -> None:
        wait = min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** attempt))
        wait *= 0.5 + random.random()   # jitter，避免多 term 同步重试
        log.debug("backoff attempt=%d sleeping %.1fs", attempt, wait)
        self._sleep(wait)

    # -- 映射 -------------------------------------------------------------
    @staticmethod
    def _to_raw_post(raw: dict) -> RawPost:
        rec = raw.get("record") or {}
        author = raw.get("author") or {}
        uri = raw["uri"]
        handle = author.get("handle")
        rkey = uri.rsplit("/", 1)[-1]
        langs = rec.get("langs") or []
        reply = rec.get("reply") or {}
        parent = reply.get("parent") or {}
        return RawPost(
            source_key=SOURCE_KEY,
            source_post_id=uri,
            author_id=author.get("did") or "",
            author_handle=handle,
            published_at=_parse_created_at(rec.get("createdAt") or raw.get("indexedAt")),
            text=rec.get("text") or "",
            lang=(langs[0] if langs else None),
            url=(f"https://bsky.app/profile/{handle}/post/{rkey}" if handle else None),
            in_reply_to=parent.get("uri"),
            reply_count=raw.get("replyCount"),
            like_count=raw.get("likeCount"),
            repost_count=raw.get("repostCount"),
            raw_payload=raw,
        )


def load_cyber_search_terms() -> tuple[list[str], str]:
    """从共用词表读 search_terms（HANDOFF §M4：采集词表即 cyber 判定词表）。"""
    import yaml

    p = Path(__file__).resolve().parents[1] / "labelers" / "lexicons" / "cyber.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    terms: list[str] = []
    for _lang, lst in (d.get("search_terms") or {}).items():
        terms.extend(lst)
    # 去重但保持顺序，保证 query_spec 可复现
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out, d["version"]
