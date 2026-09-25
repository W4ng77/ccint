-- 009 — interaction 实验的采集层。见 docs/interaction_experiment/SPEC.md §6–§10。
--
-- [MUST] 邻域是「以冻结种子为条件的扩展」,与原始语料是两个抽样过程:
--   原始语料 ≈ P(被 source/query 采到),邻域 ≈ P(被观测到的交互 | cyber 种子)。
-- 因此邻域帖**不进 posts**:进了就会改变历史语料的分母,并被判定门当成新语料去标。
--
-- [MUST] CollectionFailure ≠ ObservedZeroInteraction。采集失败的种子必须能与
-- 「采到了、确实为零」区分开 —— 这就是 interaction_seed_outcomes 存在的理由。

ALTER TABLE collection_runs DROP CONSTRAINT IF EXISTS collection_runs_mode_check;
ALTER TABLE collection_runs ADD CONSTRAINT collection_runs_mode_check
    CHECK (mode = ANY (ARRAY['backfill', 'incremental', 'oracle', 'neighborhood']));

-- 每次邻域采集的运行级统计。started_at / ended_at / status 在 collection_runs 上。
CREATE TABLE neighborhood_runs (
    run_id               bigint      PRIMARY KEY REFERENCES collection_runs(run_id),
    purpose              text        NOT NULL,        -- pilot | pilot_repeat | pilot_pagination | full
    manifest_version     text        NOT NULL,
    manifest_sha256      text        NOT NULL,
    requested_seed_count integer     NOT NULL,
    reply_depth          integer     NOT NULL,
    page_limit           integer     NOT NULL,
    endpoints_used       text[]      NOT NULL,
    request_count        integer,
    success_seed_count   integer,
    partial_seed_count   integer,
    failed_seed_count    integer,
    unavailable_seed_count integer,
    retry_count          integer,
    rate_limit_count     integer,
    snapshot_count       integer,
    edge_count           integer,
    git_commit           text        NOT NULL,
    collector_version    text        NOT NULL,
    finished_at          timestamptz
);

-- 原始 API 响应。raw_body 为响应体原始字节的 gzip,sha256 对解压后的字节计算,
-- 因此可逐字节核对;raw_payload 是同一内容的 jsonb,仅供查询(jsonb 不保留键序)。
CREATE TABLE interaction_snapshots (
    snapshot_id          bigserial   PRIMARY KEY,
    collection_run_id    bigint      NOT NULL REFERENCES collection_runs(run_id),
    seed_post_id         bigint      NOT NULL REFERENCES posts(post_id),
    seed_post_uri        text        NOT NULL,
    endpoint             text        NOT NULL,
    request_params_json  jsonb       NOT NULL,
    retrieved_at         timestamptz NOT NULL,
    http_status          integer,                     -- NULL = 传输层失败,无响应
    success              boolean     NOT NULL,
    partial              boolean     NOT NULL,
    error_type           text,
    error_message        text,
    attempts             integer     NOT NULL,
    cursor               text,                        -- 本页请求所用 cursor
    page_number          integer     NOT NULL,
    raw_body             bytea,
    raw_payload          jsonb,
    raw_payload_sha256   text,
    git_commit           text        NOT NULL,
    collector_version    text        NOT NULL
);
CREATE INDEX ON interaction_snapshots (collection_run_id, seed_post_id);
CREATE INDEX ON interaction_snapshots (seed_post_uri);

-- 每个 (run, 种子, endpoint) 的最终结局。
--   success      全部页成功取回
--   partial      取回了,但可证实不完整(中途失败 / 页数上限 / 返回数少于计数)
--   unavailable  种子本身不可得(删除、屏蔽、账号不可用)—— 不是零交互
--   failed       采集失败,结果未知 —— 不是零交互
CREATE TABLE interaction_seed_outcomes (
    collection_run_id    bigint      NOT NULL REFERENCES collection_runs(run_id),
    seed_post_id         bigint      NOT NULL REFERENCES posts(post_id),
    endpoint             text        NOT NULL,
    outcome              text        NOT NULL
        CHECK (outcome IN ('success', 'partial', 'unavailable', 'failed')),
    n_pages              integer     NOT NULL,
    n_items              integer,
    detail               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (collection_run_id, seed_post_id, endpoint)
);

-- 从快照派生的边。帖子用 AT URI 标识(邻域帖不在 posts 表)。
-- edge_key 是与快照无关的稳定身份:同一关系在两次采集里 edge_key 相同、
-- snapshot_id 不同 —— 去重按 edge_key,溯源按 snapshot_id。
CREATE TABLE interaction_edges (
    edge_id              bigserial   PRIMARY KEY,
    snapshot_id          bigint      NOT NULL REFERENCES interaction_snapshots(snapshot_id),
    collection_run_id    bigint      NOT NULL REFERENCES collection_runs(run_id),
    seed_post_id         bigint      NOT NULL REFERENCES posts(post_id),
    edge_key             text        NOT NULL,
    edge_type            text        NOT NULL
        CHECK (edge_type IN ('author', 'reply', 'quote', 'repost')),
    root_post_uri        text,
    source_post_uri      text,       -- reply/quote:子帖;author:NULL;repost:NULL
    target_post_uri      text,       -- reply:父帖;quote:被引帖;author/repost:帖子
    source_actor_id      text,
    target_actor_id      text,
    event_created_at     timestamptz,-- repost 恒为 NULL(API 不给时间,不以观测时间代替)
    observed_at          timestamptz NOT NULL,
    depth                integer,    -- reply:相对种子的深度 1/2;其余 NULL
    derivation_version   text        NOT NULL,
    code_commit          text        NOT NULL,
    metadata_json        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (snapshot_id, edge_key, derivation_version)
);
CREATE INDEX ON interaction_edges (seed_post_id, edge_type);
CREATE INDEX ON interaction_edges (edge_key);

-- 只追加:快照与边一经写入不得改删。确需清理时须显式 DROP 触发器,留下痕迹。
CREATE FUNCTION interaction_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP;
END $$;
CREATE TRIGGER interaction_snapshots_append_only
    BEFORE UPDATE OR DELETE ON interaction_snapshots
    FOR EACH ROW EXECUTE FUNCTION interaction_append_only();
CREATE TRIGGER interaction_edges_append_only
    BEFORE UPDATE OR DELETE ON interaction_edges
    FOR EACH ROW EXECUTE FUNCTION interaction_append_only();
