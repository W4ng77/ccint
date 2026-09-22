-- collection_runs.mode 增加 'oracle'。
--
-- oracle 是一次**测量用**的定向采集：拿已知答案（登记表里的受害组织名）去平台
-- 上搜，回答「如果我们早知道要找什么，能找到多少」。它把两种失败分开：
-- 平台上根本没有讨论 vs 有讨论但我们的查询词够不着。
--
-- [MUST] 它必须写 collection_runs（P5）。任何触达采集 API 的路径都要留痕，
-- 否则日后无法区分「那天量少」是社会现象还是我们在跑别的东西。原先的
-- CHECK 只允许 backfill / incremental，等于逼着测量性采集绕过 run bookkeeping。
ALTER TABLE collection_runs DROP CONSTRAINT IF EXISTS collection_runs_mode_check;
ALTER TABLE collection_runs ADD CONSTRAINT collection_runs_mode_check
    CHECK (mode = ANY (ARRAY['backfill', 'incremental', 'oracle']));
