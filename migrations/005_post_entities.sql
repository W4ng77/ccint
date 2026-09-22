-- 帖级实体表（T3 的产物，T8 / T9 的输入）。
--
-- 为什么单独建表而不是塞进 post_labels.matched_terms：实体要被按 canonical
-- 聚合（查询词扩展候选）、被按 post 反查（事件链接）。JSONB 里做这两件事要么
-- 扫全表要么建 GIN 索引，不如一张窄表。
--
-- [MUST] char_start 由 str.find 事后算出，不是模型给的。小模型算不准字符偏移，
-- 而把模型给的偏移当真会让下游的高亮和人工复核悄悄错位。
CREATE TABLE IF NOT EXISTS post_entities (
    post_id       bigint NOT NULL,
    label_version text   NOT NULL,
    entity_text   text   NOT NULL,
    entity_type   text   NOT NULL,
    canonical     text   NOT NULL,
    char_start    integer,
    PRIMARY KEY (post_id, label_version, entity_text)
);
CREATE INDEX IF NOT EXISTS idx_postent_canon ON post_entities (canonical);
CREATE INDEX IF NOT EXISTS idx_postent_ver ON post_entities (label_version);
