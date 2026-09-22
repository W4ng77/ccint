"""Pilot 统计（HANDOFF §5a）：产出决定后续架构的那个数字。

[MUST] 按天列出而非只给均值 —— 均值会掩盖 collector 中断和周末效应。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 所有按天分桶都依赖 session TimeZone=UTC（见 migrations/001_init.sql）。
_DAILY = """
SELECT (p.published_at AT TIME ZONE 'UTC')::date       AS day,
       count(*)                                        AS n_posts,
       count(*) FILTER (WHERE l.is_relevant)           AS n_relevant,
       count(DISTINCT p.author_id) FILTER (WHERE l.is_relevant) AS n_rel_authors,
       count(*) FILTER (WHERE l.is_relevant AND l.is_low_information) AS n_rel_lowinfo
FROM posts p
JOIN post_labels l ON l.post_id = p.post_id AND l.label_version = %(v)s
GROUP BY 1 ORDER BY 1
"""

_TOTALS = """
SELECT count(*)                                  AS n_posts,
       count(*) FILTER (WHERE l.is_cyber)        AS n_cyber,
       count(*) FILTER (WHERE l.is_canada)       AS n_canada,
       count(*) FILTER (WHERE l.is_relevant)     AS n_relevant,
       count(*) FILTER (WHERE l.is_low_information) AS n_low_info,
       count(*) FILTER (WHERE l.is_relevant AND l.is_low_information) AS n_rel_low_info,
       count(DISTINCT p.author_id)               AS n_authors,
       count(DISTINCT p.author_id) FILTER (WHERE l.is_relevant) AS n_rel_authors,
       min(p.published_at)                       AS first_post,
       max(p.published_at)                       AS last_post
FROM posts p
JOIN post_labels l ON l.post_id = p.post_id AND l.label_version = %(v)s
"""

_BY_LANG = """
SELECT COALESCE(p.lang,'(unknown)') AS lang, count(*) AS n
FROM posts p JOIN post_labels l ON l.post_id=p.post_id AND l.label_version=%(v)s
WHERE l.is_relevant GROUP BY 1 ORDER BY n DESC
"""

_BY_TOPIC = """
SELECT COALESCE(l.topic_key,'other') AS topic_key,
       count(*) AS n,
       count(DISTINCT p.author_id) AS n_authors
FROM posts p JOIN post_labels l ON l.post_id=p.post_id AND l.label_version=%(v)s
WHERE l.is_relevant GROUP BY 1 ORDER BY n DESC
"""

# [MUST] backfill 与 incremental 的采样性质不同，必须能拆开看（HANDOFF §3.3）。
_BY_MODE = """
SELECT r.mode,
       count(*) AS n_posts,
       count(*) FILTER (WHERE l.is_relevant) AS n_relevant
FROM posts p
JOIN post_labels l ON l.post_id=p.post_id AND l.label_version=%(v)s
LEFT JOIN collection_runs r ON r.run_id = p.first_seen_run
GROUP BY 1 ORDER BY 1
"""

_URLS = """
SELECT count(DISTINCT u) AS n_unique_urls
FROM posts p
JOIN post_labels l ON l.post_id=p.post_id AND l.label_version=%(v)s
CROSS JOIN LATERAL unnest(p.urls) AS u
WHERE l.is_relevant
"""

_RUNS = """
SELECT mode, status, count(*) AS n, sum(n_error) AS errors
FROM collection_runs GROUP BY 1,2 ORDER BY 1,2
"""


def pilot_stats(conn, label_version: str) -> dict:
    p = {"v": label_version}
    totals = dict(conn.execute(_TOTALS, p).fetchone())
    daily = [dict(r) for r in conn.execute(_DAILY, p).fetchall()]
    out = {
        "label_version": label_version,
        "totals": totals,
        "daily": daily,
        "by_lang": [dict(r) for r in conn.execute(_BY_LANG, p).fetchall()],
        "by_topic": [dict(r) for r in conn.execute(_BY_TOPIC, p).fetchall()],
        "by_mode": [dict(r) for r in conn.execute(_BY_MODE, p).fetchall()],
        "unique_urls": conn.execute(_URLS, p).fetchone()["n_unique_urls"],
        "runs": [dict(r) for r in conn.execute(_RUNS, p).fetchall()],
    }

    # 该数字是 v0 最重要的单项产出。只统计有采集覆盖的天，避免被空洞拉低。
    rel_days = [d for d in daily if d["n_posts"] > 0]
    n_rel = sum(d["n_relevant"] for d in rel_days)
    out["relevant_per_day"] = {
        "n_days_with_data": len(rel_days),
        "total_relevant": n_rel,
        "mean": (n_rel / len(rel_days)) if rel_days else 0.0,
        "median": _median([d["n_relevant"] for d in rel_days]),
        "min": min((d["n_relevant"] for d in rel_days), default=0),
        "max": max((d["n_relevant"] for d in rel_days), default=0),
    }
    return out


def _median(xs: list[int]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def format_pilot_stats(st: dict) -> str:
    """人读版。[MUST] 按天列出。"""
    t = st["totals"]
    L = []
    A = L.append
    A(f"# Pilot stats — label_version={st['label_version']}")
    A("")
    A(f"采集总量        : {t['n_posts']:,}")
    A(f"  is_cyber      : {t['n_cyber']:,}")
    A(f"  is_canada     : {t['n_canada']:,}")
    A(f"  is_relevant   : {t['n_relevant']:,}  ({_pct(t['n_relevant'], t['n_posts'])})")
    A(f"  low_information: {t['n_low_info']:,} ({_pct(t['n_low_info'], t['n_posts'])})"
      f"   其中 relevant: {t['n_rel_low_info']:,}")
    A(f"unique authors  : {t['n_authors']:,}   (relevant: {t['n_rel_authors']:,})")
    A(f"unique URLs (rel): {st['unique_urls']:,}")
    A(f"时间跨度        : {t['first_post']} .. {t['last_post']}")
    A("")
    r = st["relevant_per_day"]
    A(f">>> relevant posts/day: mean={r['mean']:.1f} median={r['median']:.1f} "
      f"min={r['min']} max={r['max']} (n_days={r['n_days_with_data']})")
    A("")
    A("## 按天（[MUST] 不只给均值 —— 均值会掩盖 collector 中断与周末效应）")
    A("")
    A("| day | posts | relevant | rel authors | rel low-info |")
    A("|---|---:|---:|---:|---:|")
    for d in st["daily"]:
        A(f"| {d['day']} | {d['n_posts']:,} | {d['n_relevant']} | "
          f"{d['n_rel_authors']} | {d['n_rel_lowinfo']} |")
    A("")
    A("## 语言分布（relevant）")
    A("")
    for x in st["by_lang"]:
        A(f"- {x['lang']}: {x['n']}")
    A("")
    A("## Topic 分布（relevant）")
    A("")
    A("| topic | posts | authors |")
    A("|---|---:|---:|")
    for x in st["by_topic"]:
        A(f"| {x['topic_key']} | {x['n']} | {x['n_authors']} |")
    A("")
    A("## 按采集 mode 拆分（[MUST] backfill 与 incremental 采样性质不同）")
    A("")
    for x in st["by_mode"]:
        A(f"- {x['mode'] or '(unknown)'}: posts={x['n_posts']:,} relevant={x['n_relevant']}")
    A("")
    A("## 采集 run 健康")
    A("")
    for x in st["runs"]:
        A(f"- {x['mode']}/{x['status']}: {x['n']} run(s), errors={x['errors'] or 0}")
    return "\n".join(L)


def _pct(a: int, b: int) -> str:
    return f"{(100.0 * a / b):.2f}%" if b else "n/a"
