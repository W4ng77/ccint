"""窗口对比趋势检出（HANDOFF §5b）。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..models import TrendCandidate

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"
TREND_METHOD = "window_compare_v1"


def _sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def detect_trends(
    conn,
    *,
    label_version: str,
    as_of: datetime,
    window_days: int = 7,
    min_posts: int = 5,
    include_low_information: bool = True,
    exclude_authors: set[str] | None = None,
) -> list[TrendCandidate]:
    """产出 candidate trend 排名。

    min_posts 挡掉小样本下的虚假暴涨（2 → 6 不是 200% 增长，是噪声）。
    排序规则 [MUST 简单透明可解释]：先按 abs_delta 降序，同值按 rel_growth 降序
    （None 视为最低），再按 topic_key 保证确定性。不使用任何 composite score。
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be tz-aware (HANDOFF §1: 时间一律 UTC)")

    rows = conn.execute(
        _sql("trend.sql"),
        {
            "as_of": as_of,
            "window_days": window_days,
            "label_version": label_version,
            "include_low_information": include_low_information,
            "exclude_authors": sorted(exclude_authors) if exclude_authors else None,
        },
    ).fetchall()

    cands: list[TrendCandidate] = []
    for r in rows:
        if r["cur_posts"] < min_posts:
            continue
        cands.append(
            TrendCandidate(
                topic_key=r["topic_key"],
                cur_posts=r["cur_posts"],
                prev_posts=r["prev_posts"],
                cur_authors=r["cur_authors"],
                prev_authors=r["prev_authors"],
                abs_delta=r["abs_delta"],
                rel_growth=r["rel_growth"],
                cur_share=r["cur_share"],
                prev_share=r["prev_share"],
                share_delta=r["cur_share"] - r["prev_share"],
            )
        )

    cands.sort(
        key=lambda c: (c.abs_delta, c.rel_growth if c.rel_growth is not None else -1e9,
                       c.topic_key),
        reverse=True,
    )
    log.info("detect_trends: %d candidate(s) above min_posts=%d", len(cands), min_posts)
    return cands


def window_bounds(as_of: datetime, window_days: int) -> dict:
    from datetime import timedelta
    return {
        "cur_start": as_of - timedelta(days=window_days),
        "cur_end": as_of,
        "prev_start": as_of - timedelta(days=2 * window_days),
        "prev_end": as_of - timedelta(days=window_days),
    }
