-- 外部事件登记表 + 评测集 + 标签溯源（T0 / T2）。
--
-- 为什么必须有这张表：在此之前，这个系统所有的 precision / recall 都挂在
-- 「写 labeler 的同一个作者所做的标注」上。κ=0.870 量的是一致性，不是独立性。
-- external_events 是第一个由外部机构发布、带时间戳、本系统无法影响的参照物，
-- 它给「我们看见了多少」这个问题提供了一个不是自己造的分母。
--
-- [MUST] published_at_utc 是**登记方**的时间，不是我们抓到的时间。两者的差
-- 正是 lead/lag 分析的基础；混用会把「我们发现得晚」记成「事件发生得晚」。
CREATE TABLE IF NOT EXISTS external_events (
    event_id          text        PRIMARY KEY,
    source            text        NOT NULL,       -- ransomware.live / rss:cbc / ...
    published_at_utc  timestamptz NOT NULL,
    discovered_at_utc timestamptz,
    title             text        NOT NULL,
    country           text,
    event_type        text,                       -- ransomware_victim / advisory / news
    url               text,
    raw_payload       jsonb       NOT NULL,       -- P1：逐字保留登记方原始记录
    ingested_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_extev_pub ON external_events (published_at_utc);
CREATE INDEX IF NOT EXISTS idx_extev_country ON external_events (country);

-- 事件里可用于文本匹配的实体。canonical 是归一化形式（小写、去标点、去法人后缀）。
CREATE TABLE IF NOT EXISTS external_event_entities (
    event_id     text NOT NULL REFERENCES external_events(event_id) ON DELETE CASCADE,
    entity_text  text NOT NULL,
    entity_type  text NOT NULL,       -- org / domain / place
    canonical    text NOT NULL,
    PRIMARY KEY (event_id, entity_text)
);
CREATE INDEX IF NOT EXISTS idx_extent_canon ON external_event_entities (canonical);

-- 评测集。一旦建立即冻结：要改就建新 eval_set_id。
--
-- [MUST] 评测集的定义里不得出现任何被评测系统的输出。否则该系统在这个集上的
-- 指标是构造出来的而不是测出来的 —— 例如用 rules_v2.is_canada=false 去筛负例，
-- 会让 rules_v2 的 precision 恒等于 1。
CREATE TABLE IF NOT EXISTS eval_sets (
    eval_set_id  text        NOT NULL,
    post_id      bigint      NOT NULL REFERENCES posts(post_id),
    label        boolean     NOT NULL,     -- 该 post 是否应判 relevant
    source       text        NOT NULL,     -- 标签怎么来的
    stratum      text,                     -- 分层键（见下）
    evidence     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (eval_set_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_evalsets_stratum ON eval_sets (eval_set_id, stratum);

-- 标签溯源侧表（T2）。
--
-- 为什么是侧表而不是给 post_labels 加列：P2 规定标签行只 INSERT 不 UPDATE。
-- 给历史 llm_v1 行补 prompt 哈希是一次 UPDATE，会让 P2 出现例外，而例外一旦
-- 开口就不再是硬约束。侧表让补溯源变成 INSERT，P2 保持绝对。
CREATE TABLE IF NOT EXISTS label_provenance (
    post_id        bigint NOT NULL,
    label_version  text   NOT NULL,
    prompt_id      text,
    prompt_version text,
    prompt_sha256  text,
    model_id       text,
    quant          text,
    params         jsonb,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, label_version)
);
