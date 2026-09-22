-- 作者画像（v1 Phase 6）。
--
-- 为什么单独建表：作者是独立于 post 的实体，其属性（受众规模、账号性质）
-- 随时间变化，且判定方法会迭代 —— 与 post_labels 同理，必须带 version。
--
-- [MUST] 两个轴**分开存**，不得合并成一个 is_bot 布尔：
--   audience  —— 实测（互动量，已控制采集时滞）
--   function  —— 内容判定（这个账号在做什么）
-- 之前的 is_broadcast 把两者压成一个判断，于是自动化情报 feed（ecrime.ch，
-- 报的是真实受害者事件）与会议宣传号（bsidesedmonton）被当成同一类剔除。
-- 它们的研究价值完全不同：前者是事件 ground truth，后者是生态指标。
CREATE TABLE IF NOT EXISTS author_profiles (
    author_id        text        NOT NULL,
    profile_version  text        NOT NULL,
    author_handle    text,

    -- 轴 1：受众（实测）
    n_posts          integer     NOT NULL,
    n_settled        integer     NOT NULL,   -- 采集时滞 >= 阈值、互动已沉淀的帖子数
    mean_engagement  double precision,       -- NULL = 无沉淀帖，不可判定
    zero_rate        double precision,
    audience         text,                   -- engaged | unheard | unknown

    -- 轴 2：账号性质（内容判定）
    function         text,                   -- incident_feed | news_media | promotion
                                             -- | individual | org_other | unknown
    function_conf    double precision,
    rationale        text,

    -- 派生类型（两轴的组合，供分析层直接使用）
    actor_type       text,

    evidence         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    profiled_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (author_id, profile_version)
);

CREATE INDEX IF NOT EXISTS idx_author_profiles_type
    ON author_profiles (profile_version, actor_type);
CREATE INDEX IF NOT EXISTS idx_author_profiles_fn
    ON author_profiles (profile_version, function);
