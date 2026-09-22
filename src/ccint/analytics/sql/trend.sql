-- 相邻窗口对比。current = [as_of - window_days, as_of)
--                previous = [as_of - 2*window_days, as_of - window_days)
--
-- [MUST] v0 只有 count 和 unique authors 两个信号（HANDOFF §5b）。
-- 不做 z-score / moving average / composite score —— 没有 baseline 模型
-- （周末效应、平台 volume 漂移、单链接 mass repost）时，这些统计量产出的是
-- 噪声而非信号，做了反而制造虚假信心。
--
-- [MUST] 必须给出 unique_authors：一个人刷 100 条帖会造成假爆发，author 数能立刻暴露。
WITH bounds AS (
    SELECT %(as_of)s::timestamptz                                            AS as_of,
           %(as_of)s::timestamptz - make_interval(days => %(window_days)s)   AS cur_start,
           %(as_of)s::timestamptz - make_interval(days => 2 * %(window_days)s) AS prev_start
),
rel AS (
    SELECT p.author_id,
           p.published_at,
           COALESCE(l.topic_key, 'other') AS topic_key,
           CASE WHEN p.published_at >= b.cur_start THEN 'cur' ELSE 'prev' END AS bucket
    FROM posts p
    JOIN post_labels l ON l.post_id = p.post_id
    CROSS JOIN bounds b
    WHERE l.label_version = %(label_version)s
      AND l.is_relevant
      AND (%(include_low_information)s OR NOT l.is_low_information)
      AND p.published_at >= b.prev_start
      AND p.published_at <  b.as_of
      -- 广播型账号剔除（见 analytics/broadcast.py）。NULL / 空数组 = 不剔除。
      AND (%(exclude_authors)s::text[] IS NULL
           OR NOT (p.author_id = ANY(%(exclude_authors)s::text[])))
),
agg AS (
    SELECT topic_key,
           count(*) FILTER (WHERE bucket = 'cur')                       AS cur_posts,
           count(*) FILTER (WHERE bucket = 'prev')                      AS prev_posts,
           count(DISTINCT author_id) FILTER (WHERE bucket = 'cur')      AS cur_authors,
           count(DISTINCT author_id) FILTER (WHERE bucket = 'prev')     AS prev_authors
    FROM rel
    GROUP BY topic_key
),
totals AS (
    SELECT GREATEST(sum(cur_posts), 0)  AS cur_total,
           GREATEST(sum(prev_posts), 0) AS prev_total
    FROM agg
)
SELECT a.topic_key,
       a.cur_posts::int,
       a.prev_posts::int,
       a.cur_authors::int,
       a.prev_authors::int,
       (a.cur_posts - a.prev_posts)::int AS abs_delta,
       -- [MUST] prev=0 时返回 NULL，不得写成 inf
       CASE WHEN a.prev_posts > 0
            THEN (a.cur_posts - a.prev_posts)::float8 / a.prev_posts
            ELSE NULL END AS rel_growth,
       CASE WHEN t.cur_total  > 0 THEN a.cur_posts::float8  / t.cur_total  ELSE 0 END AS cur_share,
       CASE WHEN t.prev_total > 0 THEN a.prev_posts::float8 / t.prev_total ELSE 0 END AS prev_share
FROM agg a CROSS JOIN totals t
ORDER BY a.topic_key
