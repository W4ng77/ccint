-- health_check.sql — HANDOFF §M8。
-- [OUT OF SCOPE] 不引入 Prometheus / Grafana：一条 SQL 覆盖 v0 阶段 90% 的监控需求。
--
-- 用法：psql "$(ccint db psql)" -f scripts/health_check.sql
--       或 ccint health

\echo '== 1. 各 source 最近一次成功采集 =='
SELECT source_key,
       max(ended_at) FILTER (WHERE status = 'success')          AS last_success,
       now() - max(ended_at) FILTER (WHERE status = 'success')  AS since_last_success
FROM collection_runs GROUP BY source_key ORDER BY source_key;

\echo ''
\echo '== 2. 最近 7 天每天的 run 数与 status 分布 =='
SELECT (started_at AT TIME ZONE 'UTC')::date AS day,
       count(*)                              AS runs,
       count(*) FILTER (WHERE status='success') AS ok,
       count(*) FILTER (WHERE status='partial') AS partial,
       count(*) FILTER (WHERE status='failed')  AS failed,
       count(*) FILTER (WHERE status='running') AS running
FROM collection_runs
WHERE started_at >= now() - interval '7 days'
GROUP BY 1 ORDER BY 1 DESC;

\echo ''
\echo '== 3. 错误率 =='
SELECT mode,
       count(*)                                        AS runs,
       sum(n_error)                                    AS total_errors,
       round(100.0 * count(*) FILTER (WHERE status IN ('failed','partial'))
             / NULLIF(count(*), 0), 2)                 AS pct_degraded
FROM collection_runs
WHERE started_at >= now() - interval '7 days'
GROUP BY mode ORDER BY mode;

\echo ''
\echo '== 4. 最近的 error_detail =='
SELECT run_id, mode, status, started_at, left(error_detail, 160) AS error_detail
FROM collection_runs
WHERE error_detail IS NOT NULL
ORDER BY started_at DESC LIMIT 5;

\echo ''
\echo '== 5. posts 新鲜度：最新 published_at 与 collected_at 的间隔 =='
SELECT max(published_at)              AS latest_published,
       max(collected_at)              AS latest_collected,
       now() - max(published_at)      AS lag_behind_now,
       max(collected_at) - max(published_at) AS collect_lag
FROM posts;

\echo ''
\echo '== 6. 采集空洞：最近 14 天里没有任何 post 的日期 =='
SELECT d::date AS missing_day
FROM generate_series(now() - interval '14 days', now(), interval '1 day') d
WHERE NOT EXISTS (
    SELECT 1 FROM posts
    WHERE published_at >= d::date AND published_at < d::date + 1
) AND d::date < now()::date
ORDER BY 1;
