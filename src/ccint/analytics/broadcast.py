"""广播型账号识别（v1 Phase 3）。

动机
----
第一波分析实测：**3% 的账号贡献 24.3% 的 relevant 语料**，而它们的平均互动量
只有其余账号的 1/27（0.20 vs 5.34）。它们是两类：

1. 自动化威胁情报 feed —— `ecrime.ch`、`cyberintelligence`、`ransomlook`、
   `falconfeedsio`：勒索软件泄露站的监控机器人，每出现一个新受害者就自动发一条。
2. 安全会议宣传号 —— `bsidesedmonton`、`hackfest`：反复推送 CFP 与活动公告。

这两类都不是「公众讨论」。把它们算进 topic volume，等于用机器人的发帖频率
去度量社会关注度。实测 `ransomware` 这个 topic 45 条里 40 条来自 5 个 feed —— 
剔除后该 topic 直接从排行榜上消失。

为什么用行为定义而不是 handle 名单
--------------------------------
写死 handle 的名单会腐烂（新 bot 随时出现），且不可复现到别的时间窗。
这里只用三个可观测行为：发帖量、活跃天数、零互动率。

[MUST] 必须控制采集时滞
-----------------------
`like_count` 是**采集那一刻的快照**。incremental 在发帖后约 4 小时就抓走，
backfill 平均滞后 354 小时 —— 同一条帖子在两种模式下测到的点赞数能差 2.4 倍。
如果不加限制，「零互动」会把**刚采到的帖子**误判成广播号。

因此零互动率只统计 `collected_at - published_at >= min_lag_hours` 的帖子。
实测该混杂并不驱动结论（广播号的滞后反而更长：359h vs 323h；
只看滞后 >168h 的帖子，零互动率 76.7% vs 50.0%，均赞 0.19 vs 3.15），
但这个守卫必须留着，否则换一批数据就会翻车。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

BROADCAST_METHOD = "behavioral_v1"

DEFAULTS = {"min_posts": 5, "min_days": 4, "zero_rate": 0.6, "min_lag_hours": 24}

SQL = """
WITH scoped AS (
    SELECT p.author_id, p.author_handle, p.published_at,
           p.like_count, p.repost_count,
           extract(epoch FROM (p.collected_at - p.published_at)) / 3600.0 AS lag_h
    FROM posts p
    JOIN post_labels l ON l.post_id = p.post_id
    WHERE l.label_version = %(label_version)s
      AND l.is_relevant
      AND (%(since)s::timestamptz IS NULL OR p.published_at >= %(since)s)
      AND (%(until)s::timestamptz IS NULL OR p.published_at <  %(until)s)
),
agg AS (
    SELECT author_id,
           max(author_handle)                              AS author_handle,
           count(*)                                        AS n_posts,
           count(DISTINCT published_at::date)              AS n_days,
           avg(like_count + repost_count)                  AS avg_engagement,
           -- [MUST] 零互动率只看已充分沉淀的帖子，避免把新采到的帖子误判
           count(*) FILTER (WHERE lag_h >= %(min_lag_hours)s)                  AS n_settled,
           count(*) FILTER (WHERE lag_h >= %(min_lag_hours)s
                              AND like_count = 0 AND repost_count = 0)         AS n_settled_zero
    FROM scoped GROUP BY author_id
)
SELECT *,
       CASE WHEN n_settled > 0
            THEN n_settled_zero::float8 / n_settled END AS zero_rate
FROM agg
ORDER BY n_posts DESC
"""


@dataclass(frozen=True)
class BroadcastAuthor:
    author_id: str
    author_handle: str
    n_posts: int
    n_days: int
    zero_rate: float
    avg_engagement: float


@dataclass
class BroadcastResult:
    method: str
    params: dict
    authors: list[BroadcastAuthor]
    n_total_posts: int
    n_total_authors: int

    @property
    def author_ids(self) -> set[str]:
        return {a.author_id for a in self.authors}

    @property
    def n_posts(self) -> int:
        return sum(a.n_posts for a in self.authors)

    @property
    def post_share(self) -> float:
        return self.n_posts / self.n_total_posts if self.n_total_posts else 0.0

    @property
    def author_share(self) -> float:
        return len(self.authors) / self.n_total_authors if self.n_total_authors else 0.0


def detect_broadcast_authors(
    conn, *, label_version: str,
    since: datetime | None = None, until: datetime | None = None,
    min_posts: int = DEFAULTS["min_posts"],
    min_days: int = DEFAULTS["min_days"],
    zero_rate: float = DEFAULTS["zero_rate"],
    min_lag_hours: int = DEFAULTS["min_lag_hours"],
) -> BroadcastResult:
    """行为判定：发帖量高 + 跨多日持续 + 已沉淀帖子的零互动率高。

    三个条件必须同时满足：
      n_posts >= min_posts   —— 排除偶发
      n_days  >= min_days    —— 排除一次性刷屏（那是另一种问题，不是广播号）
      zero_rate >= 阈值      —— 没有受众，即「发出去但没人接」
    """
    rows = [dict(r) for r in conn.execute(SQL, {
        "label_version": label_version, "since": since, "until": until,
        "min_lag_hours": min_lag_hours,
    }).fetchall()]

    hits = [
        BroadcastAuthor(
            author_id=r["author_id"], author_handle=r["author_handle"],
            n_posts=r["n_posts"], n_days=r["n_days"],
            zero_rate=float(r["zero_rate"]), avg_engagement=float(r["avg_engagement"]),
        )
        for r in rows
        if r["n_posts"] >= min_posts and r["n_days"] >= min_days
        and r["zero_rate"] is not None and r["zero_rate"] >= zero_rate
    ]
    hits.sort(key=lambda a: (-a.n_posts, a.author_handle))

    res = BroadcastResult(
        method=BROADCAST_METHOD,
        params={"min_posts": min_posts, "min_days": min_days,
                "zero_rate": zero_rate, "min_lag_hours": min_lag_hours},
        authors=hits,
        n_total_posts=sum(r["n_posts"] for r in rows),
        n_total_authors=len(rows),
    )
    log.info("broadcast: %d/%d authors (%.1f%%) contributing %d/%d posts (%.1f%%)",
             len(hits), res.n_total_authors, 100 * res.author_share,
             res.n_posts, res.n_total_posts, 100 * res.post_share)
    return res
