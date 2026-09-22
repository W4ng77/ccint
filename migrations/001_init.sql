-- 001_init.sql — v0 schema. 四张表，照 HANDOFF §4 原样。
--
-- [MUST] 时间一律 UTC。新建 cluster 会继承宿主机时区（实测为 America/Toronto），
-- 而 date_trunc('day', <timestamptz>) 使用 session TimeZone 分桶 —— 若不强制 UTC，
-- §5a 的「按天 relevant posts」会按本地日界切分，且跨 DST 出现 23/25 小时的桶。
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone TO ''UTC''', current_database());
END
$$;

-- 采集留痕。没有这张表就无法区分 system failure 与 social trend。
CREATE TABLE collection_runs (
    run_id         BIGSERIAL PRIMARY KEY,
    source_key     TEXT        NOT NULL,
    mode           TEXT        NOT NULL CHECK (mode IN ('backfill','incremental')),
    query_version  TEXT        NOT NULL,
    query_spec     JSONB       NOT NULL,
    window_start   TIMESTAMPTZ,
    window_end     TIMESTAMPTZ,
    cursor_start   TEXT,
    cursor_end     TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at       TIMESTAMPTZ,
    status         TEXT        NOT NULL CHECK (status IN ('running','success','partial','failed')),
    n_fetched      INT         NOT NULL DEFAULT 0,
    n_inserted     INT         NOT NULL DEFAULT 0,
    n_duplicate    INT         NOT NULL DEFAULT 0,
    n_error        INT         NOT NULL DEFAULT 0,
    error_detail   TEXT
);
CREATE INDEX ON collection_runs (source_key, started_at DESC);

-- 统一后的帖子。source-independent。raw_payload 永不丢弃。
CREATE TABLE posts (
    post_id          BIGSERIAL PRIMARY KEY,
    source_key       TEXT        NOT NULL,
    source_post_id   TEXT        NOT NULL,
    author_id        TEXT        NOT NULL,
    author_handle    TEXT,
    published_at     TIMESTAMPTZ NOT NULL,
    collected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen_run   BIGINT      REFERENCES collection_runs(run_id),
    text             TEXT        NOT NULL,
    lang             TEXT,
    url              TEXT,
    in_reply_to      TEXT,
    reply_count      INT,
    like_count       INT,
    repost_count     INT,
    urls             TEXT[]      NOT NULL DEFAULT '{}',
    text_hash        TEXT        NOT NULL,   -- normalized text 的 sha256，用于精确去重
    raw_payload      JSONB       NOT NULL,
    UNIQUE (source_key, source_post_id)
);
CREATE INDEX ON posts (published_at);
CREATE INDEX ON posts (source_key, published_at);
CREATE INDEX ON posts (author_id);
CREATE INDEX ON posts (text_hash);

-- 所有判定结果。带版本，可并存，可重算。
CREATE TABLE post_labels (
    post_id            BIGINT      NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    label_version      TEXT        NOT NULL,
    is_cyber           BOOLEAN     NOT NULL,
    is_canada          BOOLEAN     NOT NULL,
    is_relevant        BOOLEAN     NOT NULL,   -- v0 = is_cyber AND is_canada
    topic_key          TEXT,
    is_low_information BOOLEAN     NOT NULL DEFAULT FALSE,
    matched_terms      JSONB       NOT NULL DEFAULT '{}',
    confidence         REAL,
    labeled_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, label_version)
);
CREATE INDEX ON post_labels (label_version, topic_key);
CREATE INDEX ON post_labels (label_version, is_relevant);

-- 每份报告的 provenance。回答「这个结论是哪套参数产出的」。
CREATE TABLE analysis_runs (
    analysis_id    BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    as_of          TIMESTAMPTZ NOT NULL,
    label_version  TEXT        NOT NULL,
    trend_method   TEXT        NOT NULL,
    window_days    INT         NOT NULL,
    filters        JSONB       NOT NULL DEFAULT '{}',
    agent_model    TEXT,
    prompt_version TEXT,
    report_path    TEXT,
    result         JSONB       NOT NULL DEFAULT '{}'
);
