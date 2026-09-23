-- CVE 与开源情报关联层。
--
-- 帖子里提到一个 CVE 编号,本身只是一个字符串。要让它变成情报，需要知道这个
-- 漏洞是什么、多严重、是否已被实际利用 —— 而这三件事都来自系统之外的权威源
-- （NVD、CISA KEV）。这张表把「社媒在谈什么」和「这个漏洞客观上是什么」接起来。
--
-- [MUST] known_exploited 来自 CISA KEV，与 CVSS 分数是**正交**的信息。
-- 一个 CVSS 5.3 但已被实际利用的漏洞，比 CVSS 9.8 但无人利用的更值得关注。
-- 不要把两者合成一个「风险分」——那会把外部事实和我们的权重混在一起。
CREATE TABLE IF NOT EXISTS cve_records (
    cve_id            text        PRIMARY KEY,
    published_at      timestamptz,
    last_modified_at  timestamptz,
    description       text,
    cvss_score        double precision,
    cvss_severity     text,
    cvss_version      text,
    known_exploited   boolean     NOT NULL DEFAULT false,  -- CISA KEV
    kev_date_added    date,
    source            text        NOT NULL,                -- nvd / kev / unresolved
    raw_payload       jsonb,
    fetched_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cve_kev ON cve_records (known_exploited);
CREATE INDEX IF NOT EXISTS idx_cve_pub ON cve_records (published_at);

-- 帖子 ↔ CVE。一条帖可以提多个 CVE。
--
-- [MUST] 记录 char_start，与 post_entities 同理：位置由 re.finditer 精确给出，
-- 不依赖任何模型输出，因此这张表是**确定性**的，可以独立于标注质量被信任。
CREATE TABLE IF NOT EXISTS post_cves (
    post_id     bigint  NOT NULL,
    cve_id      text    NOT NULL,
    char_start  integer NOT NULL,
    PRIMARY KEY (post_id, cve_id, char_start)
);
CREATE INDEX IF NOT EXISTS idx_postcve_cve ON post_cves (cve_id);
CREATE INDEX IF NOT EXISTS idx_postcve_post ON post_cves (post_id);
