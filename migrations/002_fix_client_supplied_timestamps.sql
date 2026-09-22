-- 002 — 用 indexedAt 修正不可信的 createdAt。
--
-- 背景：Bluesky 的 record.createdAt 由客户端自填，可为任意值；indexedAt 由服务端
-- 在入索引时赋值。实测 52,085 条语料中有 53 条被回填 >2 天（最远 2016-04），
-- 238 条带未来时间戳。本项目以「同一 topic 在不同时间窗口的变化」为核心，
-- 这类时间戳会直接落进错误的窗口，表现为凭空出现的 trend。
--
-- 本 migration 不重采数据 —— raw_payload 里两个时间戳都在，故清洗逻辑可重跑。
-- 这正是 HANDOFF §1.1「Raw 永不丢弃」保护的场景。
--
-- 容忍度与 ingest.resolve_published_at 保持一致：
--   回填容忍 2 天（跨 PDS 联邦同步的真实延迟）
--   未来容忍 1 小时（客户端时钟偏移）
UPDATE posts
SET published_at = (raw_payload->>'indexedAt')::timestamptz
WHERE raw_payload ? 'indexedAt'
  AND (
        published_at > (raw_payload->>'indexedAt')::timestamptz + interval '1 hour'
     OR published_at < (raw_payload->>'indexedAt')::timestamptz - interval '2 days'
  );
