"""Agent 的三个工具（HANDOFF §M6，签名固定）。

[MUST] Agent 不判断什么算 trend —— 这些工具只读，不能改变排名或分析对象。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_STATS = """
SELECT count(*)                       AS n_posts,
       count(DISTINCT p.author_id)    AS n_authors,
       count(*) FILTER (WHERE l.is_low_information) AS n_low_info,
       COALESCE(sum(p.like_count), 0) AS total_likes,
       COALESCE(sum(p.repost_count), 0) AS total_reposts,
       count(DISTINCT p.lang)         AS n_langs,
       min(p.published_at)            AS first_post,
       max(p.published_at)            AS last_post
FROM posts p JOIN post_labels l ON l.post_id = p.post_id
WHERE l.label_version = %(v)s AND l.is_relevant AND l.topic_key = %(topic)s
  AND p.published_at >= %(start)s AND p.published_at < %(end)s
"""

_LANGS = """
SELECT COALESCE(p.lang,'(unknown)') AS lang, count(*) AS n
FROM posts p JOIN post_labels l ON l.post_id = p.post_id
WHERE l.label_version = %(v)s AND l.is_relevant AND l.topic_key = %(topic)s
  AND p.published_at >= %(start)s AND p.published_at < %(end)s
GROUP BY 1 ORDER BY n DESC LIMIT 5
"""

# 确定性选取：engagement 排序，排除 is_low_information。
# 作者多样性在 Python 侧强制（见 get_representative_posts）。
_REPRESENTATIVE = """
SELECT p.post_id, p.url, p.published_at, p.author_handle, p.author_id, p.text,
       COALESCE(p.like_count,0) AS like_count,
       COALESCE(p.repost_count,0) AS repost_count,
       COALESCE(p.reply_count,0) AS reply_count,
       (COALESCE(p.like_count,0) + COALESCE(p.repost_count,0) * 2
        + COALESCE(p.reply_count,0)) AS engagement
FROM posts p JOIN post_labels l ON l.post_id = p.post_id
WHERE l.label_version = %(v)s AND l.is_relevant AND l.topic_key = %(topic)s
  AND NOT l.is_low_information
  AND p.published_at >= %(start)s AND p.published_at < %(end)s
ORDER BY engagement DESC, p.post_id ASC
"""


def _parse_as_of(as_of: str | datetime) -> datetime:
    if isinstance(as_of, datetime):
        dt = as_of
    else:
        dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TopicTools:
    """工具实现。绑定一个连接与 label_version，agent 无法越过它们查别的数据。"""

    def __init__(self, conn, label_version: str):
        self.conn = conn
        self.v = label_version
        self.served_post_ids: set[int] = set()   # 供 M6 的 post_id 幻觉校验
        self.served_post_urls: dict[int, str | None] = {}

    # -- tool 1 -----------------------------------------------------------
    def get_topic_stats(self, topic_key: str, window_days: int, as_of: str) -> dict:
        end = _parse_as_of(as_of)
        start = end - timedelta(days=window_days)
        p = {"v": self.v, "topic": topic_key, "start": start, "end": end}
        row = dict(self.conn.execute(_STATS, p).fetchone())
        row["langs"] = [dict(r) for r in self.conn.execute(_LANGS, p).fetchall()]
        row["topic_key"] = topic_key
        row["window_start"] = start.isoformat()
        row["window_end"] = end.isoformat()
        row["posts_per_author"] = (
            round(row["n_posts"] / row["n_authors"], 2) if row["n_authors"] else None
        )
        return _jsonable(row)

    # -- tool 2 -----------------------------------------------------------
    def get_representative_posts(self, topic_key: str, window_days: int,
                                 as_of: str, k: int = 10) -> list[dict]:
        """[MUST] 确定性选取：engagement top-k，强制覆盖 ≥3 个不同 author，
        排除 is_low_information。每条必须带 post_id / url / published_at /
        author_handle / text。
        """
        end = _parse_as_of(as_of)
        start = end - timedelta(days=window_days)
        rows = [dict(r) for r in self.conn.execute(
            _REPRESENTATIVE,
            {"v": self.v, "topic": topic_key, "start": start, "end": end},
        ).fetchall()]
        if not rows:
            return []

        # 先按 engagement 取，再补作者多样性：若 top-k 的作者数 < 3，
        # 用后续不同作者的最高 engagement 帖替换同作者的重复项。
        picked: list[dict] = []
        seen_authors: list[str] = []
        for r in rows:
            if len(picked) >= k:
                break
            picked.append(r)
            seen_authors.append(r["author_id"])

        distinct = len(set(seen_authors))
        if distinct < 3 and len(picked) >= 3:
            remaining = [r for r in rows if r not in picked]
            for cand in remaining:
                if cand["author_id"] in {p["author_id"] for p in picked}:
                    continue
                # 替换掉出现次数最多的作者的最后一条
                counts: dict[str, int] = {}
                for p in picked:
                    counts[p["author_id"]] = counts.get(p["author_id"], 0) + 1
                worst = max(counts, key=lambda a: counts[a])
                if counts[worst] <= 1:
                    break
                for i in range(len(picked) - 1, -1, -1):
                    if picked[i]["author_id"] == worst:
                        picked[i] = cand
                        break
                if len({p["author_id"] for p in picked}) >= 3:
                    break

        out = []
        for r in picked:
            self.served_post_ids.add(int(r["post_id"]))
            self.served_post_urls[int(r["post_id"])] = r["url"]
            out.append(_jsonable({
                "post_id": r["post_id"], "url": r["url"],
                "published_at": r["published_at"], "author_handle": r["author_handle"],
                "text": r["text"], "like_count": r["like_count"],
                "repost_count": r["repost_count"], "reply_count": r["reply_count"],
            }))
        return out

    # -- tool 3 -----------------------------------------------------------
    def compare_periods(self, topic_key: str, as_of: str, window_days: int) -> dict:
        end = _parse_as_of(as_of)
        cur = self.get_topic_stats(topic_key, window_days, end.isoformat())
        prev_end = end - timedelta(days=window_days)
        prev = self.get_topic_stats(topic_key, window_days, prev_end.isoformat())
        growth = (
            (cur["n_posts"] - prev["n_posts"]) / prev["n_posts"]
            if prev["n_posts"] else None
        )
        return {
            "topic_key": topic_key,
            "window_days": window_days,
            "current": cur,
            "previous": prev,
            "abs_delta": cur["n_posts"] - prev["n_posts"],
            "rel_growth": growth,          # prev=0 时为 None，不是 inf
            "author_delta": cur["n_authors"] - prev["n_authors"],
        }


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [_jsonable(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


# Anthropic tool 定义。strict=true 保证参数 schema 合法。
TOOL_SCHEMAS = [
    {
        "name": "get_topic_stats",
        "description": "取某 topic 在 [as_of - window_days, as_of) 窗口内的统计："
                       "帖数、unique author 数、语言分布、engagement 合计、"
                       "posts_per_author（刷量指标）。",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_key": {"type": "string"},
                "window_days": {"type": "integer"},
                "as_of": {"type": "string", "description": "ISO8601 UTC"},
            },
            "required": ["topic_key", "window_days", "as_of"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_representative_posts",
        "description": "取该 topic 窗口内代表性帖子：按 engagement 排序，"
                       "已排除低信息量帖，且强制覆盖至少 3 个不同作者。"
                       "返回的 post_id 是你唯一可以在 evidence 中引用的来源。",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_key": {"type": "string"},
                "window_days": {"type": "integer"},
                "as_of": {"type": "string"},
                "k": {"type": "integer"},
            },
            "required": ["topic_key", "window_days", "as_of", "k"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compare_periods",
        "description": "对比当前窗口与前一个等长窗口的统计，用于判断上升是真实放量"
                       "还是基数效应。",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_key": {"type": "string"},
                "as_of": {"type": "string"},
                "window_days": {"type": "integer"},
            },
            "required": ["topic_key", "as_of", "window_days"],
            "additionalProperties": False,
        },
    },
]
