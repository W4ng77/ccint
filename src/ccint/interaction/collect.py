"""邻域采集:以冻结种子为条件,取回复线程、引用、转发者。见 SPEC §7–§13。

[MUST] 只负责把 API 响应原样落库并记下每个种子的结局;边在 derive.py 里从快照
派生,可离线重跑。不过滤、不做相关性判断、不回填任何缺失值。

与 collectors/bluesky.py 的 ``_get`` 分开写请求循环,是因为快照要求保留**每一次**
请求的 HTTP 状态与原始响应体(包括错误响应),而 ``_get`` 在失败时只抛异常。
鉴权、token 过期判定、rate-limit 等待沿用 bluesky.py 的实现。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from psycopg.types.json import Jsonb

from .. import db
from ..collectors.bluesky import (
    BlueskySession, _BASE_BACKOFF, _MAX_ATTEMPTS, _MAX_BACKOFF, _is_token_error,
    BlueskyCollector,
)
from ..config import REPO_ROOT, settings
from .manifest import Manifest

log = logging.getLogger(__name__)

COLLECTOR_VERSION = "neighborhood_v1"
EP_THREAD = "app.bsky.feed.getPostThread"
EP_QUOTES = "app.bsky.feed.getQuotes"
EP_REPOSTS = "app.bsky.feed.getRepostedBy"
ENDPOINTS = [EP_THREAD, EP_QUOTES, EP_REPOSTS]
REPLY_DEPTH = 2
MAX_PAGES = 50          # 单个种子单个 endpoint 的翻页上限;触顶记为 partial


def git_commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=REPO_ROOT).stdout.strip()


@dataclass
class Response:
    status: int | None
    body: bytes | None
    data: dict | None
    attempts: int
    error_type: str | None = None
    error_message: str | None = None


@dataclass
class RunStats:
    requests: int = 0
    retries: int = 0
    rate_limits: int = 0
    snapshots: int = 0
    outcomes: dict = field(default_factory=dict)   # seed_post_id -> {endpoint: outcome}


def classify_error(status: int | None, data: dict | None) -> str:
    """把 API 错误归到可统计的类别。NotFound 单列:它表示种子不可得,而非采集失败。"""
    err = (data or {}).get("error") or ""
    if status is None:
        return "transport"
    if err == "NotFound":
        return "not_found"
    if err in ("BlockedActor", "BlockedByActor"):
        return "blocked"
    if status == 429:
        return "rate_limited"
    if status in (400, 401) and err in ("ExpiredToken", "InvalidToken", "AuthMissing"):
        return "auth"
    if status == 400:
        return f"invalid_request:{err or 'unknown'}"
    if status >= 500:
        return "server_error"
    return f"http_{status}"


class NeighborhoodCollector:
    def __init__(self, *, client: httpx.Client | None = None, sleep=time.sleep):
        self.client = client or httpx.Client(
            base_url=settings.bluesky_base_url, timeout=30.0,
            headers={"User-Agent": "ccint/0.1 (research; Canada cyber social intelligence)"})
        self.session = BlueskySession(settings, self.client)
        self._sleep = sleep
        self.stats = RunStats()

    def close(self) -> None:
        self.client.close()

    # -- HTTP ------------------------------------------------------------
    def request(self, endpoint: str, params: dict) -> Response:
        """重试 transport / 5xx / 429 / token 过期;其余错误立即返回并保留响应体。"""
        token = self.session.access_token()
        last: Response | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if attempt > 1:
                self.stats.retries += 1
            try:
                r = self.client.get(f"/xrpc/{endpoint}", params=params,
                                    headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                self.stats.requests += 1
                last = Response(None, None, None, attempt, "transport", str(e)[:500])
                self._backoff(attempt)
                continue
            self.stats.requests += 1
            body = r.content
            try:
                data = r.json()
            except ValueError:
                data = None
            if r.status_code == 200 and data is not None:
                return Response(200, body, data, attempt)
            et = classify_error(r.status_code, data)
            last = Response(r.status_code, body, data, attempt, et, r.text[:500])
            if r.status_code in (400, 401) and _is_token_error(r):
                token = self.session.refresh()
                continue
            if r.status_code == 429:
                self.stats.rate_limits += 1
                self._sleep(BlueskyCollector._rate_limit_wait(r))
                continue
            if r.status_code >= 500:
                self._backoff(attempt)
                continue
            return last
        return last

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** (attempt - 1))))

    # -- 采集 ------------------------------------------------------------
    def collect_seed(self, cur, run_id: int, seed: dict, *, page_limit: int,
                     commit: str) -> dict[str, str]:
        pid, uri = int(seed["post_id"]), seed["post_uri"]
        out = {}

        def snap(endpoint, params, resp, page, cursor, success, partial):
            body = resp.body
            cur.execute("""
                INSERT INTO interaction_snapshots
                  (collection_run_id, seed_post_id, seed_post_uri, endpoint, request_params_json,
                   retrieved_at, http_status, success, partial, error_type, error_message,
                   attempts, cursor, page_number, raw_body, raw_payload, raw_payload_sha256,
                   git_commit, collector_version)
                VALUES (%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, pid, uri, endpoint, Jsonb(params), resp.status, success, partial,
                 resp.error_type, resp.error_message, resp.attempts, cursor, page,
                 gzip.compress(body, mtime=0) if body is not None else None,
                 Jsonb(resp.data) if resp.data is not None else None,
                 hashlib.sha256(body).hexdigest() if body is not None else None,
                 commit, COLLECTOR_VERSION))
            self.stats.snapshots += 1

        def outcome(endpoint, oc, n_pages, n_items, detail):
            cur.execute("""
                INSERT INTO interaction_seed_outcomes
                  (collection_run_id, seed_post_id, endpoint, outcome, n_pages, n_items, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, pid, endpoint, oc, n_pages, n_items, Jsonb(detail)))
            out[endpoint] = oc

        # 1) 回复线程:一次请求,无分页
        params = {"uri": uri, "depth": REPLY_DEPTH, "parentHeight": 0}
        resp = self.request(EP_THREAD, params)
        if resp.status == 200:
            gap = thread_gap(resp.data)
            partial = gap["returned_depth1"] < gap["reply_count"]
            snap(EP_THREAD, params, resp, 1, None, True, partial)
            root_type = (resp.data.get("thread") or {}).get("$type", "")
            if not root_type.endswith("#threadViewPost"):
                outcome(EP_THREAD, "unavailable", 1, 0, {"root_type": root_type, **gap})
            else:
                outcome(EP_THREAD, "partial" if partial else "success", 1,
                        gap["returned_nodes"], gap)
        else:
            snap(EP_THREAD, params, resp, 1, None, False, False)
            oc = "unavailable" if resp.error_type in ("not_found", "blocked") else "failed"
            outcome(EP_THREAD, oc, 1, None, {"error_type": resp.error_type})

        # 2) 引用 / 3) 转发者:cursor 分页
        for ep, key, count_key in ((EP_QUOTES, "posts", "quoteCount"),
                                   (EP_REPOSTS, "repostedBy", "repostCount")):
            cursor, page, items, seen = None, 0, 0, set()
            while True:
                page += 1
                params = {"uri": uri, "limit": page_limit}
                if cursor:
                    params["cursor"] = cursor
                resp = self.request(ep, params)
                if resp.status != 200:
                    snap(ep, params, resp, page, cursor, False, False)
                    if page == 1:
                        oc = ("unavailable" if resp.error_type in ("not_found", "blocked")
                              else "failed")
                    else:
                        oc = "partial"
                    outcome(ep, oc, page, items if page > 1 else None,
                            {"error_type": resp.error_type, "failed_page": page})
                    break
                batch = resp.data.get(key) or []
                ids = [b.get("uri") or b.get("did") for b in batch]
                dup = sum(1 for i in ids if i in seen)
                seen.update(ids)
                items += len(batch)
                nxt = resp.data.get("cursor")
                more = bool(nxt) and bool(batch)
                capped = more and page >= MAX_PAGES
                snap(ep, params, resp, page, cursor, True, capped)
                if not more or capped:
                    outcome(ep, "partial" if capped else "success", page, len(seen),
                            {"cap_hit": capped, "duplicate_items": dup if page > 1 else 0})
                    break
                cursor = nxt
        return out


def thread_gap(data: dict) -> dict:
    """返回线程里实际拿到的节点数与种子自报的 replyCount,用于判定 partial。"""
    t = data.get("thread") or {}
    post = t.get("post") or {}
    reps = t.get("replies") or []
    d1 = len(reps)
    d2 = sum(len(r.get("replies") or []) for r in reps)
    kinds: dict[str, int] = {}
    for r in reps + [x for r in reps for x in (r.get("replies") or [])]:
        k = r.get("$type", "?").rsplit("#", 1)[-1]
        kinds[k] = kinds.get(k, 0) + 1
    return {"reply_count": int(post.get("replyCount") or 0), "returned_depth1": d1,
            "returned_depth2": d2, "returned_nodes": d1 + d2, "node_types": kinds}


def run(manifest: Manifest, seeds: list[dict], *, purpose: str,
        page_limit: int = 100) -> int:
    """执行一次邻域采集 run,返回 run_id。每个种子单独提交,中断不丢已完成部分。"""
    commit = git_commit()
    spec = {"manifest_version": manifest.version, "manifest_sha256": manifest.sha256,
            "purpose": purpose, "reply_depth": REPLY_DEPTH, "page_limit": page_limit,
            "endpoints": ENDPOINTS, "seed_post_ids": [int(s["post_id"]) for s in seeds]}
    with db.connect(autocommit=True) as c:
        run_id = c.execute("""
            INSERT INTO collection_runs (source_key, mode, query_version, query_spec, status)
            VALUES ('bluesky', 'neighborhood', %s, %s, 'running') RETURNING run_id""",
            (COLLECTOR_VERSION, Jsonb(spec))).fetchone()["run_id"]
        c.execute("""
            INSERT INTO neighborhood_runs (run_id, purpose, manifest_version, manifest_sha256,
              requested_seed_count, reply_depth, page_limit, endpoints_used, git_commit,
              collector_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, purpose, manifest.version, manifest.sha256, len(seeds), REPLY_DEPTH,
             page_limit, ENDPOINTS, commit, COLLECTOR_VERSION))

    col = NeighborhoodCollector()
    status, err = "success", None
    try:
        for i, s in enumerate(seeds, 1):
            with db.transaction() as conn, conn.cursor() as cur:
                col.stats.outcomes[int(s["post_id"])] = col.collect_seed(
                    cur, run_id, s, page_limit=page_limit, commit=commit)
            if i % 10 == 0:
                log.warning("neighborhood run %d: %d/%d seeds, %d requests",
                            run_id, i, len(seeds), col.stats.requests)
    except Exception as e:                              # noqa: BLE001
        status, err = "failed", f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        col.close()
        per_seed = [set(v.values()) for v in col.stats.outcomes.values()]
        n_fail = sum(1 for v in per_seed if "failed" in v)
        n_unav = sum(1 for v in per_seed if "unavailable" in v and "failed" not in v)
        n_part = sum(1 for v in per_seed if "partial" in v and not v & {"failed", "unavailable"})
        n_ok = sum(1 for v in per_seed if v == {"success"})
        if status == "success" and (n_fail or len(per_seed) < len(seeds)):
            status = "partial"
        with db.connect(autocommit=True) as c:
            c.execute("""
                UPDATE neighborhood_runs SET request_count=%s, success_seed_count=%s,
                  partial_seed_count=%s, failed_seed_count=%s, unavailable_seed_count=%s,
                  retry_count=%s, rate_limit_count=%s, snapshot_count=%s, finished_at=now()
                WHERE run_id=%s""",
                (col.stats.requests, n_ok, n_part, n_fail, n_unav, col.stats.retries,
                 col.stats.rate_limits, col.stats.snapshots, run_id))
            c.execute("""
                UPDATE collection_runs SET ended_at=now(), status=%s, n_fetched=%s,
                  n_inserted=%s, n_error=%s, error_detail=%s WHERE run_id=%s""",
                (status, col.stats.requests, col.stats.snapshots, n_fail, err, run_id))
    return run_id
