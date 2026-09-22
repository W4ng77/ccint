"""图表的数据层。

[MUST] 所有查询都接受 exclude_authors，并且**同一张图里 raw 与 organic 必须
来自同一次查询**——分两次查再拼接，会在窗口边界上产生不一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# --------------------------------------------------------------------------
# 1. 每个 topic 的 raw / organic / authors（招牌图的数据）
# --------------------------------------------------------------------------
TOPIC_SPLIT_SQL = """
WITH rel AS (
    SELECT p.author_id, p.published_at,
           COALESCE(l.topic_key, 'other') AS topic_key,
           (p.author_id = ANY(%(bcast)s::text[])) AS is_bcast
    FROM posts p
    JOIN post_labels l ON l.post_id = p.post_id
    WHERE l.label_version = %(label_version)s
      AND l.is_relevant
      AND (%(since)s::timestamptz IS NULL OR p.published_at >= %(since)s)
      AND (%(until)s::timestamptz IS NULL OR p.published_at <  %(until)s)
)
SELECT topic_key,
       count(*)                                          AS raw_posts,
       count(*) FILTER (WHERE NOT is_bcast)              AS organic_posts,
       count(DISTINCT author_id)                         AS raw_authors,
       count(DISTINCT author_id) FILTER (WHERE NOT is_bcast) AS organic_authors
FROM rel
GROUP BY topic_key
ORDER BY raw_posts DESC
"""

# --------------------------------------------------------------------------
# 2. 按天的轨迹
# --------------------------------------------------------------------------
TRAJECTORY_SQL = """
WITH rel AS (
    SELECT p.author_id, p.published_at::date AS d,
           COALESCE(l.topic_key, 'other') AS topic_key,
           (p.author_id = ANY(%(bcast)s::text[])) AS is_bcast
    FROM posts p
    JOIN post_labels l ON l.post_id = p.post_id
    WHERE l.label_version = %(label_version)s
      AND l.is_relevant
      AND (%(since)s::timestamptz IS NULL OR p.published_at >= %(since)s)
      AND (%(until)s::timestamptz IS NULL OR p.published_at <  %(until)s)
),
daily AS (
    SELECT topic_key, d,
           count(*) AS raw_posts,
           count(*) FILTER (WHERE NOT is_bcast) AS organic_posts,
           count(DISTINCT author_id) AS authors
    FROM rel GROUP BY topic_key, d
),
totals AS (SELECT d, sum(raw_posts) AS day_total FROM daily GROUP BY d)
SELECT dd.topic_key, dd.d, dd.raw_posts, dd.organic_posts, dd.authors,
       t.day_total,
       CASE WHEN t.day_total > 0
            THEN dd.raw_posts::float8 / t.day_total ELSE 0 END AS share
FROM daily dd JOIN totals t USING (d)
ORDER BY dd.topic_key, dd.d
"""

# --------------------------------------------------------------------------
# 3. topic × 周 热力图（share，并按周归一）
# --------------------------------------------------------------------------
HEATMAP_SQL = """
WITH rel AS (
    SELECT date_trunc('week', p.published_at)::date AS wk,
           COALESCE(l.topic_key, 'other') AS topic_key
    FROM posts p
    JOIN post_labels l ON l.post_id = p.post_id
    WHERE l.label_version = %(label_version)s
      AND l.is_relevant
      AND NOT (p.author_id = ANY(%(bcast)s::text[]))
      AND (%(since)s::timestamptz IS NULL OR p.published_at >= %(since)s)
      AND (%(until)s::timestamptz IS NULL OR p.published_at <  %(until)s)
),
cells AS (SELECT wk, topic_key, count(*) n FROM rel GROUP BY 1,2),
wtot  AS (SELECT wk, sum(n) tot FROM cells GROUP BY 1)
SELECT c.wk, c.topic_key, c.n, w.tot,
       c.n::float8 / NULLIF(w.tot,0) AS share
FROM cells c JOIN wtot w USING (wk)
ORDER BY c.wk, c.topic_key
"""

# --------------------------------------------------------------------------
# 4. actor–topic 二部图
# --------------------------------------------------------------------------
ACTOR_TOPIC_SQL = """
SELECT p.author_id, max(p.author_handle) AS author_handle,
       COALESCE(l.topic_key, 'other') AS topic_key,
       count(*) AS n_posts,
       avg(p.like_count + p.repost_count) AS avg_engagement,
       (p.author_id = ANY(%(bcast)s::text[])) AS is_bcast
FROM posts p
JOIN post_labels l ON l.post_id = p.post_id
WHERE l.label_version = %(label_version)s
  AND l.is_relevant
  AND (%(since)s::timestamptz IS NULL OR p.published_at >= %(since)s)
  AND (%(until)s::timestamptz IS NULL OR p.published_at <  %(until)s)
GROUP BY p.author_id, COALESCE(l.topic_key, 'other'), is_bcast
ORDER BY n_posts DESC
"""

# --------------------------------------------------------------------------
# 5. 作者集中度（Lorenz / Gini）
# --------------------------------------------------------------------------
CONCENTRATION_SQL = """
SELECT p.author_id, max(p.author_handle) AS author_handle, count(*) AS n_posts,
       (p.author_id = ANY(%(bcast)s::text[])) AS is_bcast
FROM posts p
JOIN post_labels l ON l.post_id = p.post_id
WHERE l.label_version = %(label_version)s AND l.is_relevant
GROUP BY p.author_id, is_bcast
ORDER BY n_posts DESC
"""

# --------------------------------------------------------------------------
# 6. 共享外链（co-sharing）—— diffusion 的可行替代
# --------------------------------------------------------------------------
COSHARE_SQL = """
WITH rel AS (
    SELECT p.author_id, max(p.author_handle) AS author_handle, p.post_id, p.urls,
           bool_or(p.author_id = ANY(%(bcast)s::text[])) AS is_bcast
    FROM posts p
    JOIN post_labels l ON l.post_id = p.post_id
    WHERE l.label_version = %(label_version)s AND l.is_relevant
      AND p.urls IS NOT NULL AND array_length(p.urls, 1) > 0
    GROUP BY p.author_id, p.post_id, p.urls
),
dom AS (
    SELECT author_id, author_handle, is_bcast,
           -- 同一信源的多个入口归并，否则 youtu.be 与 youtube.com 会被算成两个信源
           CASE lower(substring(u FROM '://(?:www\\.)?([^/:]+)'))
                WHEN 'youtu.be'      THEN 'youtube.com'
                WHEN 'm.youtube.com' THEN 'youtube.com'
                ELSE lower(substring(u FROM '://(?:www\\.)?([^/:]+)'))
           END AS domain
    FROM rel, unnest(urls) u
    WHERE u ~ '^https?://'
)
SELECT domain, author_id, author_handle, is_bcast, count(*) AS n
FROM dom
WHERE domain IS NOT NULL
  -- 平台自身与短链服务不构成「共享了同一个信源」
  -- 短链服务不是信源：它们只是转发入口，会把不同来源伪装成「同一个域名」
  AND domain NOT IN ('bsky.app','bsky.social','twitter.com','x.com','t.co',
                     'bit.ly','buff.ly','dlvr.it','ift.tt','trib.al','lnkd.in',
                     'tinyurl.com','ow.ly','rebrand.ly','shorturl.at','is.gd',
                     'goo.gl','flip.it','sh.st','cutt.ly','linktr.ee')
GROUP BY 1,2,3,4
"""


@dataclass
class VizContext:
    label_version: str
    since: datetime | None
    until: datetime | None
    broadcast_ids: list[str]

    def params(self) -> dict:
        return {"label_version": self.label_version,
                "since": self.since, "until": self.until,
                "bcast": self.broadcast_ids or [""]}


def _rows(conn, sql: str, ctx: VizContext) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, ctx.params()).fetchall()]


def topic_split(conn, ctx):      return _rows(conn, TOPIC_SPLIT_SQL, ctx)
def trajectory(conn, ctx):       return _rows(conn, TRAJECTORY_SQL, ctx)
def heatmap(conn, ctx):          return _rows(conn, HEATMAP_SQL, ctx)
def actor_topic(conn, ctx):      return _rows(conn, ACTOR_TOPIC_SQL, ctx)
def concentration(conn, ctx):    return _rows(conn, CONCENTRATION_SQL, ctx)
def coshare(conn, ctx):          return _rows(conn, COSHARE_SQL, ctx)


def gini(counts: list[int]) -> float:
    """作者发帖量的基尼系数。0 = 人人均等，1 = 一个账号包办全部。"""
    xs = sorted(c for c in counts if c > 0)
    n = len(xs)
    if n == 0:
        return 0.0
    total = sum(xs)
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return (2 * cum) / (n * total) - (n + 1) / n
