"""M8 Health check。与 scripts/health_check.sql 等价，但不依赖 psql 二进制。

[OUT OF SCOPE] 不引入 Prometheus / Grafana（HANDOFF §M8）。
"""
from __future__ import annotations

from . import db

_QUERIES = [
    ("各 source 最近一次成功采集", """
        SELECT source_key,
               max(ended_at) FILTER (WHERE status='success') AS last_success,
               now() - max(ended_at) FILTER (WHERE status='success') AS since_last_success
        FROM collection_runs GROUP BY source_key ORDER BY source_key"""),
    ("最近 7 天每天的 run 数与 status 分布", """
        SELECT (started_at AT TIME ZONE 'UTC')::date AS day, count(*) AS runs,
               count(*) FILTER (WHERE status='success') AS ok,
               count(*) FILTER (WHERE status='partial') AS partial,
               count(*) FILTER (WHERE status='failed')  AS failed,
               count(*) FILTER (WHERE status='running') AS running
        FROM collection_runs WHERE started_at >= now() - interval '7 days'
        GROUP BY 1 ORDER BY 1 DESC"""),
    ("错误率（近 7 天）", """
        SELECT mode, count(*) AS runs, sum(n_error) AS total_errors,
               round(100.0 * count(*) FILTER (WHERE status IN ('failed','partial'))
                     / NULLIF(count(*),0), 2) AS pct_degraded
        FROM collection_runs WHERE started_at >= now() - interval '7 days'
        GROUP BY mode ORDER BY mode"""),
    ("最近的 error_detail", """
        SELECT run_id, mode, status, started_at, left(error_detail,120) AS error_detail
        FROM collection_runs WHERE error_detail IS NOT NULL
        ORDER BY started_at DESC LIMIT 5"""),
    ("posts 新鲜度", """
        SELECT max(published_at) AS latest_published,
               max(collected_at) AS latest_collected,
               now() - max(published_at) AS lag_behind_now
        FROM posts"""),
    ("采集空洞（近 14 天无 post 的日期）", """
        SELECT d::date AS missing_day
        FROM generate_series(now() - interval '14 days', now(), interval '1 day') d
        WHERE NOT EXISTS (SELECT 1 FROM posts
                          WHERE published_at >= d::date AND published_at < d::date + 1)
          AND d::date < now()::date
        ORDER BY 1"""),
]


def run_health_check() -> str:
    out: list[str] = []
    with db.connect(autocommit=True) as conn:
        for title, sql in _QUERIES:
            rows = conn.execute(sql).fetchall()
            out.append(f"== {title} ==")
            if not rows:
                out.append("  (无)")
            else:
                cols = list(rows[0].keys())
                out.append("  " + " | ".join(cols))
                for r in rows:
                    out.append("  " + " | ".join(str(r[c]) for c in cols))
            out.append("")
    return "\n".join(out)
