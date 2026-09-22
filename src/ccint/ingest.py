"""Ingest：normalize / 去重 / 入库 / 维护 collection_runs。

[MUST] 异常安全（HANDOFF §M3）：任何异常下 collection_runs 都必须被收尾
（failed 或 partial），不能留下永久 running 的记录。
实现要点 —— run 的记账走**独立连接**，与数据写入事务解耦。否则数据事务 abort 时
run 记录会一并回滚，正好留下无法收尾的 running 行。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from . import db
from .config import settings
from .models import RawPost

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.I)
_NUL_RE = re.compile("\x00")
_WS_RE = re.compile(r"\s+")


def strip_nul(s: str) -> str:
    """去掉 NUL 字节。

    PostgreSQL 的 text 与 jsonb 都无法存储 U+0000（UntranslatableCharacter），
    而 Bluesky 的帖子正文确实出现过它（实测在 63k 条中命中，打挂整轮采集）。
    NUL 不承载任何语义，剔除它不构成「丢弃 raw」—— 它本就无法被表示。
    """
    return s.replace("\x00", "") if "\x00" in s else s


def sanitize_payload(obj):
    """递归剔除 raw_payload 中的 NUL，使其可存入 jsonb。"""
    if isinstance(obj, str):
        return strip_nul(obj)
    if isinstance(obj, dict):
        return {strip_nul(k) if isinstance(k, str) else k: sanitize_payload(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_payload(x) for x in obj]
    return obj


# --------------------------------------------------------------------------
# Normalize
# --------------------------------------------------------------------------


def extract_urls(post: RawPost) -> list[str]:
    """text 中的 URL + facets/embed 里的完整 URL（Bluesky 正文常是截断显示文本）。"""
    urls: list[str] = list(_URL_RE.findall(post.text))
    rec = (post.raw_payload or {}).get("record") or {}
    for facet in rec.get("facets") or []:
        for feat in facet.get("features") or []:
            u = feat.get("uri")
            if u:
                urls.append(u)
    embed = (post.raw_payload or {}).get("embed") or {}
    ext = embed.get("external") or {}
    if ext.get("uri"):
        urls.append(ext["uri"])
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,;)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def strip_nul(obj):
    """递归剔除 NUL（U+0000）。

    [决策记录] PostgreSQL 的 text / jsonb 在物理上无法存储 U+0000，写入时抛
    UntranslatableCharacter。实测 Bluesky 语料中确有帖子正文含该字符
    （run 4 因此整轮失败）。这是「raw_payload 逐字节保真」的唯一例外，且无可回避：
    替代方案是把 raw_payload 存成 bytea，那会让所有 JSONB 查询失效，代价更大。
    NUL 不携带语义，剔除不损失信息。
    """
    if isinstance(obj, str):
        return _NUL_RE.sub("", obj)
    if isinstance(obj, dict):
        return {strip_nul(k): strip_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_nul(v) for v in obj]
    return obj


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", strip_nul((text or "").replace("\u00a0", " "))).strip()


def compute_text_hash(text: str) -> str:
    """小写 + 去 URL + 去多余空白后 sha256（HANDOFF §M3）。"""
    t = _URL_RE.sub(" ", text or "")
    t = _WS_RE.sub(" ", t).strip().lower()
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def detect_lang(text: str) -> str | None:
    """langdetect 兜底。短文本不可靠，直接放弃而不是猜。"""
    t = _URL_RE.sub(" ", text or "").strip()
    if len(t) < 20:
        return None
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0          # 确定性
        return detect(t)
    except Exception:  # noqa: BLE001
        return None


# indexedAt 由服务端在入索引时赋值，createdAt 由客户端自填、可为任意值。
# 实测：52,085 条中 53 条被回填 >2 天（最远到 2016 年），238 条带未来时间戳。
# 对一个以时间窗口对比为核心的项目，这类时间戳会直接落进错误的桶。
_BACKDATE_TOLERANCE = timedelta(days=2)    # 容忍跨 PDS 联邦同步的真实延迟
_FUTURE_TOLERANCE = timedelta(hours=1)     # 容忍客户端时钟偏移


def resolve_published_at(post: RawPost) -> tuple[datetime, str | None]:
    """返回 (published_at, 替换原因)。

    createdAt 与 indexedAt 严重不符时改用 indexedAt。原始两个值都留在
    raw_payload 里，故本判定随时可重算 —— 这正是「raw 永不丢弃」保护的场景。
    """
    created = post.published_at
    raw_idx = (post.raw_payload or {}).get("indexedAt")
    if not raw_idx:
        return created, None
    try:
        from .collectors.bluesky import _parse_created_at
        indexed = _parse_created_at(raw_idx)
    except Exception:  # noqa: BLE001
        return created, None
    if created > indexed + _FUTURE_TOLERANCE:
        return indexed, "future_createdAt"
    if created < indexed - _BACKDATE_TOLERANCE:
        return indexed, "backdated_createdAt"
    return created, None


def normalize(post: RawPost) -> dict:
    text = normalize_text(post.text)
    published_at, ts_fix = resolve_published_at(post)
    if ts_fix:
        log.debug("published_at replaced (%s) for %s", ts_fix, post.source_post_id)
    return {
        "source_key": post.source_key,
        "source_post_id": post.source_post_id,
        "author_id": post.author_id,
        "author_handle": strip_nul(post.author_handle or "") or None,
        "published_at": published_at,
        "text": text,
        "lang": post.lang or detect_lang(text),
        "url": strip_nul(post.url),
        "in_reply_to": strip_nul(post.in_reply_to),
        "reply_count": post.reply_count,
        "like_count": post.like_count,
        "repost_count": post.repost_count,
        "urls": [strip_nul(u) for u in extract_urls(post)],
        "text_hash": compute_text_hash(text),
        "raw_payload": Jsonb(sanitize_payload(post.raw_payload)),
    }


# --------------------------------------------------------------------------
# collection_runs 生命周期（独立连接）
# --------------------------------------------------------------------------


class RunRecorder:
    """collection_runs 的记账器。持有独立连接，autocommit，与数据事务解耦。"""

    def __init__(self, conn: psycopg.Connection, run_id: int):
        self.conn = conn
        self.run_id = run_id
        self.n_fetched = 0
        self.n_inserted = 0
        self.n_duplicate = 0
        self.n_error = 0          # 所有 HTTP 失败（HANDOFF §3.2 要求全部计入）
        self.n_incomplete = 0     # 未能跑完的 (window, term) 数 —— 这才是「本轮不完整」
        self.cursor_end: str | None = None

    def finish(self, status: str, error_detail: str | None = None) -> None:
        self.conn.execute(
            """UPDATE collection_runs
               SET status=%s, ended_at=now(), n_fetched=%s, n_inserted=%s,
                   n_duplicate=%s, n_error=%s, error_detail=%s, cursor_end=%s
               WHERE run_id=%s""",
            (status, self.n_fetched, self.n_inserted, self.n_duplicate,
             self.n_error, error_detail, self.cursor_end, self.run_id),
        )


@contextmanager
def open_run(
    *,
    source_key: str,
    mode: str,
    query_version: str,
    query_spec: dict,
    window_start: datetime | None,
    window_end: datetime | None,
):
    """开一条 run，保证任何路径下都被收尾。"""
    with db.connect(autocommit=True) as book:
        row = book.execute(
            """INSERT INTO collection_runs
               (source_key, mode, query_version, query_spec,
                window_start, window_end, status)
               VALUES (%s,%s,%s,%s,%s,%s,'running')
               RETURNING run_id""",
            (source_key, mode, query_version, Jsonb(query_spec),
             window_start, window_end),
        ).fetchone()
        rec = RunRecorder(book, row["run_id"])
        log.info("collection_run %d opened (mode=%s)", rec.run_id, mode)
        try:
            yield rec
        except BaseException as e:      # noqa: BLE001 — KeyboardInterrupt 也要收尾
            detail = f"{type(e).__name__}: {e}"[:2000]
            rec.finish("failed", detail)
            log.error("collection_run %d FAILED: %s", rec.run_id, detail)
            raise
        else:
            # [MUST] partial 的语义是「本轮不完整」（HANDOFF §3.2），不是「出现过错误」。
            # 二者必须分开：accessJwt 每 ~2 小时过期一次，刷新前那次请求必然失败并
            # 计入 n_error；若据此标 partial，则**每轮都是 partial**，
            # §4 想用 run 状态区分「系统故障 vs 社会趋势」的能力就被噪声淹没。
            status = "partial" if rec.n_incomplete else "success"
            rec.finish(status)
            log.info(
                "collection_run %d %s: fetched=%d inserted=%d dup=%d err=%d incomplete=%d",
                rec.run_id, status, rec.n_fetched, rec.n_inserted,
                rec.n_duplicate, rec.n_error, rec.n_incomplete,
            )


# --------------------------------------------------------------------------
# 写库
# --------------------------------------------------------------------------

_INSERT = """
INSERT INTO posts (source_key, source_post_id, author_id, author_handle,
                   published_at, first_seen_run, text, lang, url, in_reply_to,
                   reply_count, like_count, repost_count, urls, text_hash, raw_payload)
VALUES (%(source_key)s, %(source_post_id)s, %(author_id)s, %(author_handle)s,
        %(published_at)s, %(first_seen_run)s, %(text)s, %(lang)s, %(url)s,
        %(in_reply_to)s, %(reply_count)s, %(like_count)s, %(repost_count)s,
        %(urls)s, %(text_hash)s, %(raw_payload)s)
ON CONFLICT (source_key, source_post_id) DO NOTHING
RETURNING post_id
"""


def upsert_posts(conn: psycopg.Connection, rows: list[dict], run_id: int) -> int:
    """批量插入，返回真实新增数。ON CONFLICT DO NOTHING 做精确去重。"""
    if not rows:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            r = {**r, "first_seen_run": run_id}
            cur.execute(_INSERT, r)
            if cur.fetchone() is not None:
                inserted += 1
    return inserted


def dedup_batch(posts: list[RawPost]) -> list[RawPost]:
    """批内去重：同一帖会被多个 cyber term 命中（HANDOFF §3.4「结果合并去重」）。"""
    seen, out = set(), []
    for p in posts:
        k = (p.source_key, p.source_post_id)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------


def last_success_window_end(source_key: str) -> datetime | None:
    with db.connect(autocommit=True) as conn:
        row = conn.execute(
            """SELECT window_end FROM collection_runs
               WHERE source_key=%s AND status='success' AND window_end IS NOT NULL
               ORDER BY window_end DESC LIMIT 1""",
            (source_key,),
        ).fetchone()
    return row["window_end"] if row else None


def resolve_incremental_window(source_key: str, now: datetime | None = None
                               ) -> tuple[datetime, datetime]:
    """从上次成功 run 的 window_end 继续，向前回拨 lookback。无历史则回退默认起点。"""
    now = now or datetime.now(timezone.utc)
    ckpt = last_success_window_end(source_key)
    if ckpt:
        start = ckpt - timedelta(minutes=settings.incremental_lookback_minutes)
    else:
        start = now - timedelta(days=settings.default_since_days)
        log.info("no checkpoint for %s; falling back to %s", source_key, start)
    return start, now


# --------------------------------------------------------------------------
# Stale run 回收
# --------------------------------------------------------------------------


def reap_stale_runs(older_than_hours: int = 6) -> int:
    """把被 SIGKILL / 断电遗留的 running 记录收尾为 failed。

    [MUST] HANDOFF §M3 要求不能留下永久 running 的记录。open_run 的 except 能覆盖
    异常退出，但覆盖不了 SIGKILL —— 故需要这个启动时的回收器补上。
    """
    with db.connect(autocommit=True) as conn:
        rows = conn.execute(
            """UPDATE collection_runs
               SET status='failed', ended_at=now(),
                   error_detail=COALESCE(error_detail,'')
                     || '[reaped] 进程未收尾即退出（SIGKILL / 断电），由 reap_stale_runs 标记'
               WHERE status='running' AND started_at < now() - make_interval(hours => %s)
               RETURNING run_id""",
            (older_than_hours,),
        ).fetchall()
    if rows:
        log.warning("reaped %d stale running run(s): %s",
                    len(rows), [r["run_id"] for r in rows])
    return len(rows)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def iter_windows(since: datetime, until: datetime, chunk_days: int | None):
    """把采集窗口切成小块。

    [MUST] backfill 必须按天分块。searchPosts 结果按时间倒序、cursor 向历史翻页，
    因此一旦触到 max_pages 上限，被截断的永远是**较早的日期** —— 直接对整个 30 天
    窗口分页会系统性地少采老数据，在 trend 层表现为一条虚假的「讨论量上升」。
    按天分块后每天有独立的翻页预算，该偏差消除。
    """
    if not chunk_days:
        yield since, until
        return
    cur = since
    step = timedelta(days=chunk_days)
    while cur < until:
        nxt = min(cur + step, until)
        yield cur, nxt
        cur = nxt


def _flush(rows: list[dict], run_id: int) -> int:
    if not rows:
        return 0
    with db.transaction() as conn:
        return upsert_posts(conn, rows, run_id)


def run_collection(
    collector,
    *,
    mode: str,
    since: datetime,
    until: datetime,
    max_pages_per_term: int = 60,
    chunk_days: int | None = None,
    progress=None,
) -> dict:
    """驱动 collector → normalize → 去重 → 入库 → 收尾 run。

    每个 (chunk, term) 采完即入库：内存有界，且中途失败已采部分不丢。
    """
    if chunk_days is None:
        chunk_days = 1 if mode == "backfill" else None

    spec = {
        **collector.query_spec(),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "mode": mode,
        "chunk_days": chunk_days,
        "max_pages_per_term": max_pages_per_term,
    }

    with open_run(
        source_key=collector.source_key,
        mode=mode,
        query_version=collector.query_version,
        query_spec=spec,
        window_start=since,
        window_end=until,
    ) as rec:
        n_truncated = 0
        for ws, we in iter_windows(since, until, chunk_days):
            for term in collector.search_terms:
                batch: list[RawPost] = []
                try:
                    for page in collector.iter_term_pages(
                        term, since=ws, until=we, max_pages=max_pages_per_term
                    ):
                        batch.extend(page.posts)
                        rec.cursor_end = page.next_cursor or rec.cursor_end
                except Exception as e:  # noqa: BLE001
                    # 单个 term 失败不中断整轮，但该 (window, term) 的数据确实缺失
                    rec.n_error += 1
                    rec.n_incomplete += 1
                    log.warning("term=%r window=%s failed: %s", term, ws.date(), e)

                rec.n_fetched += len(batch)
                unique = dedup_batch(batch)
                inserted = _flush([normalize(p) for p in unique], rec.run_id)
                rec.n_inserted += inserted
                rec.n_duplicate += len(batch) - inserted

            if progress:
                progress(ws, we, rec)
            log.info(
                "window %s..%s done | cum fetched=%d inserted=%d dup=%d err=%d",
                ws.date(), we.date(), rec.n_fetched, rec.n_inserted,
                rec.n_duplicate, rec.n_error,
            )

        rec.n_error += getattr(collector, "n_http_errors", 0)
        return {
            "run_id": rec.run_id,
            "n_fetched": rec.n_fetched,
            "n_inserted": rec.n_inserted,
            "n_duplicate": rec.n_duplicate,
            "n_error": rec.n_error,
            "n_incomplete": rec.n_incomplete,
            "n_truncated_terms": n_truncated,
        }
