# HANDOFF — Canada Cyber Social Intelligence, v0 Walking Skeleton

> 这份文档是给实现方（Claude Code agent）的完整规格。请先通读全文再动手。
> 文档中标注 **[MUST]** 的是硬约束，不得以"更简单/更优雅"为由改动；
> 标注 **[OUT OF SCOPE]** 的是本阶段明确不做的事，不要顺手实现。

---

## 0. 项目背景（最小必要上下文）

长期目标是一套 longitudinal social-media intelligence system：持续采集与 Canada 相关的 cybersecurity 讨论，沉淀成带时间戳的历史语料，在此之上做 temporal trend detection，再由 LLM agent 对被检出的 trend 做 evidence-grounded 分析。

**本次交付的不是那套系统，而是它的 walking skeleton（v0）**：一条最窄的端到端竖切，用最少的组件把 `采集 → 存储 → 标注 → 趋势 → agent 分析 → 报告` 跑通一遍。v0 的目的不是发现真实 trend，而是：

1. 验证管道端到端通畅、可重复、可扩展；
2. 顺带产出一个决定后续架构的关键数字：**Bluesky 上 Canada × cybersecurity 的 relevant posts/day**。

这个数字会决定下一步是"加信号源"还是"改分析粒度"，所以 v0 必须能输出它。

### v0 的 Definition of Done

一条命令产出一份 `report.md`，内含：top-3 candidate topics、agent 对第一名的解释、每条结论可追溯到具体 post id；以及一条命令输出 pilot 统计（日均 relevant post 数、按语言/topic 分布）。

---

## 1. 不可违反的设计原则 [MUST]

这五条是整个项目后续扩展的接缝。v0 的代码结构必须让这五种扩展都是**加法**，不需要重写或重采数据。

1. **Raw 永不丢弃。** API 原始 payload 完整存入 `posts.raw_payload` (JSONB)。清洗逻辑有 bug 时必须能重跑，不能重采。
2. **采集端宽松，相关性判定在下游且带版本。** 任何 relevance / topic 判断结果写入 `post_labels`，带 `label_version`。换方法 = 写入新 version，`posts` 表一个字节都不动。旧 version 保留，用于并排比较。
3. **Topic 必须是时间不变的参照系。** trend 的定义是"同一个 topic 在不同时间窗口的变化"，所以 topic 定义在一次分析内必须固定。v0 用规则分类天然满足这点。（后续接入 embedding 聚类时，它是同一列 `topic_key` 的另一个 `label_version`，与规则基线 A/B，而不是替换。）
4. **每一次采集都要留痕。** 所有采集写入 `collection_runs`。原因：若某天数据量骤降 80%，必须能区分"社会讨论真的少了"和"collector 挂了 / API 变了"。否则会把 system failure 当成 social trend。
5. **新增 source 只写一个文件。** Collector 实现统一 Protocol，输出统一的 `RawPost`。下游所有模块（标注、趋势、agent）不得包含任何 source-specific 逻辑。

附加原则：

- **时间一律 UTC、一律 tz-aware。** 数据库全部 `TIMESTAMPTZ`。禁止 naive datetime。
- **幂等。** 所有命令重复执行不产生重复数据、不破坏已有数据。
- **不做破坏性删除。** 低质量内容用 flag 标记（`is_low_information`），不物理删除。

---

## 2. 技术栈与环境

- 运行环境：用户自有服务器（Linux，双 RTX 4080，共 32GB VRAM，无 NVLink）。
- Python 3.11+，包管理用 `uv`。
- PostgreSQL 16，用 `docker-compose` 起单服务。
- 数据库访问用 `psycopg` (v3) + **裸 SQL**。**[MUST] 不要引入 ORM。** 本项目的查询以时间窗口聚合为主，SQL 直接写更清楚，也便于后续搬进 dashboard。
- Migration 用编号的 `.sql` 文件顺序执行，不引入 Alembic。
- CLI 用 `typer`。
- 日志用标准 `logging`，结构化输出到 stdout。
- 配置用 `.env` + `pydantic-settings`。**密钥不得提交到仓库**，提供 `.env.example`。

关于 GPU：**v0 不使用 GPU**。规则标注器不需要模型，agent 走 API。GPU 的用途（encoder 分类器、embedding、批量重标注）属于 v1+，本次不要预留任何 CUDA 依赖。

---

## 3. 数据源与采集策略

### 3.1 选 Bluesky 的理由

唯一理由是**零审批延迟**——API 开放，今天就能跑。这不是"Bluesky 最适合"的结论，v0 之后会做正式的 source feasibility 比较（Reddit / X）。不要在代码或文档里把 Bluesky 写成唯一或首选 source。

### 3.2 API 注意事项 [MUST 先验证]

计划使用 AppView 的 `app.bsky.feed.searchPosts`（支持 `q` / `limit` / `cursor` / `since` / `until` / `lang`）。

**动手前必须先用 curl 或最小脚本实测确认**以下各项的当前行为，不要相信本文档对 API 细节的描述（它可能已过时）：

- 端点路径、是否需要鉴权（公共 AppView vs. 通过 `com.atproto.server.createSession` + app password 拿 token）；
- `since` / `until` 接受的格式，以及历史回溯的实际深度与完整性；
- `limit` 上限与分页 cursor 语义；
- rate limit 的实际阈值与 429 的响应形式。

可以使用官方 `atproto` Python SDK，也可以直接打 HTTP；选哪个由实测结果决定，在 README 记录理由。

**[MUST] 实现指数退避重试与 rate-limit 处理。** 任何一次 HTTP 失败都要计入 `collection_runs.n_error`，连续失败导致本轮不完整时状态标 `partial` 而非 `success`。

### 3.3 两种采集模式

| mode | 用途 | 触发 |
|---|---|---|
| `backfill` | 一次性 seed 历史语料，让 trend 层在 day 0 有数据可算 | 手动，指定 `--since/--until` |
| `incremental` | 持续采集，从上次成功的 checkpoint 继续 | 定时（cron / systemd timer） |

**[MUST] 这两种数据的采样性质不同**（search API 的历史覆盖不完整且可能有偏，与持续采集不可等同）。必须用 `collection_runs.mode` 区分并在报告中注明。v0 允许在趋势计算中混用，但报告里要有一行说明当前窗口包含 backfill 数据。

### 3.4 查询策略 [MUST]

**采集端只用 cyber 词表过滤，Canada 相关性完全放到下游标注。**

理由：collection-time filter 不可逆——没采到的帖子事后补不回来。cyber 词汇表相对稳定且足够窄，作为采集边界的风险可接受；而 Canada relevance 的定义（keyword / entity / geography / classifier / hybrid）恰恰是最可能反复演化的那一个，必须留在可重算的下游。这个不对称是有意设计。

实现：对 cyber 词表中每个 term 分别发起一次 search（Bluesky 的 `q` 不支持复杂布尔），结果合并去重。整个词表 + 参数序列化进 `collection_runs.query_spec`，并打上 `query_version`（v0 为 `"cyber_v1"`）。

**[MUST] `query_version` 变更必须显式递增并记录。** 若查询条件悄悄改动，后续看到的 trend 将无法区分"社会变化"与"搜索条件变化"。

---

## 4. 数据库 Schema

四张表。migration 文件 `migrations/001_init.sql`。

```sql
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
    matched_terms      JSONB       NOT NULL DEFAULT '{}',  -- 命中了哪些词，便于人工检查
    confidence         REAL,
    labeled_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, label_version)
);
CREATE INDEX ON post_labels (label_version, topic_key);
CREATE INDEX ON post_labels (label_version, is_relevant);

-- 每份报告的 provenance。回答"这个结论是哪套参数产出的"。
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
    result         JSONB       NOT NULL DEFAULT '{}'  -- trend candidates 快照
);
```

**[OUT OF SCOPE] 不要建 `topics` / `trend_snapshots` / `reports` 表。** v0 的 topic 由规则常量定义，trend 每次现算并快照进 `analysis_runs.result`。

---

## 5. 仓库结构

```
ccint/
├── README.md
├── HANDOFF.md                     ← 本文档
├── pyproject.toml
├── .env.example
├── docker-compose.yml             # 仅 postgres
├── migrations/
│   └── 001_init.sql
├── src/ccint/
│   ├── config.py
│   ├── db.py                      # 连接池、migration runner、事务辅助
│   ├── models.py                  # RawPost / StoredPost / Label / TrendCandidate
│   ├── collectors/
│   │   ├── base.py                # Collector Protocol
│   │   └── bluesky.py
│   ├── ingest.py                  # normalize / dedup / 写库 / 维护 collection_runs
│   ├── labelers/
│   │   ├── base.py                # Labeler Protocol
│   │   ├── rules_v1.py
│   │   └── lexicons/
│   │       ├── cyber.yaml
│   │       ├── canada.yaml
│   │       └── topics.yaml
│   ├── analytics/
│   │   ├── descriptive.py         # pilot 统计
│   │   └── trend.py               # 窗口对比
│   ├── agent/
│   │   ├── tools.py               # 三个工具
│   │   ├── analyst.py             # agent loop
│   │   └── prompts/analyst_v1.md
│   ├── report.py
│   └── cli.py
├── scripts/health_check.sql
└── tests/
```

---

## 6. 模块规格

每个模块给出：职责、接口、验收条件、明确不做的事。

### M1 — Infra & Config

**职责**：docker-compose 起 Postgres；migration runner；配置加载；`db.py` 提供连接与事务。

**验收**：`docker compose up -d && ccint db migrate` 后四张表存在，重复执行 migrate 不报错、不重复建表。

**不做**：连接池调优、读写分离、备份策略。

---

### M2 — Collector（Bluesky）

**职责**：按 cyber 词表分页拉取，产出统一的 `RawPost`。不写库，不做去重，不做任何相关性判断。

**接口** [MUST 保持签名稳定]：

```python
# models.py
@dataclass(frozen=True)
class RawPost:
    source_key: str
    source_post_id: str
    author_id: str
    author_handle: str | None
    published_at: datetime          # tz-aware UTC
    text: str
    lang: str | None
    url: str | None
    in_reply_to: str | None
    reply_count: int | None
    like_count: int | None
    repost_count: int | None
    raw_payload: dict

@dataclass(frozen=True)
class Page:
    posts: list[RawPost]
    next_cursor: str | None

# collectors/base.py
class Collector(Protocol):
    source_key: str
    query_version: str
    def query_spec(self) -> dict: ...
    def fetch_page(
        self, *,
        since: datetime | None,
        until: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> Page: ...
```

**验收**：单测用录制的 fixture 响应，断言字段映射正确、时间为 UTC tz-aware、分页终止条件正确、429 触发退避。

**不做**：任何过滤、清洗、Canada 判断。Collector 只负责"把东西原样拿回来"。

---

### M3 — Ingest

**职责**：开 `collection_runs`（status=`running`）→ 驱动 collector 分页 → normalize → 去重 → 批量 upsert → 收尾写统计与 status。

**Normalize 规则**：去首尾空白、统一空白字符、抽取 URL 存入 `urls[]`、语言检测缺失时用 `langdetect` 兜底、计算 `text_hash`（小写 + 去 URL + 去多余空白后 sha256）。

**去重**：`(source_key, source_post_id)` 唯一约束做精确去重，冲突时 `ON CONFLICT DO NOTHING` 并计入 `n_duplicate`。

**Checkpoint**：`incremental` 模式从该 source 最近一次 `status='success'` 的 run 的 `window_end` 继续；无历史则回退到配置的默认起点。

**[MUST] 异常安全**：任何异常下 `collection_runs` 都必须被收尾（`failed` 或 `partial`），不能留下永久 `running` 的记录。

**验收**：同一时间窗连续跑两次，第二次 `n_inserted=0`、`n_duplicate` 等于第一次的 `n_inserted`。

**不做**：near-duplicate 聚类（`text_hash` 精确匹配足够，语义近重复属于 v1+）。

---

### M4 — Labeler v1（规则）

**职责**：对 post 判定 `is_cyber` / `is_canada` / `topic_key` / `is_low_information`，写入 `post_labels`，`label_version = "rules_v1"`。

**接口**：

```python
class Labeler(Protocol):
    label_version: str
    def label(self, text: str, lang: str | None, meta: dict) -> Label: ...
```

**词表放 YAML，不硬编码在 Python 里** [MUST]——词表要能被非程序员审阅和修改，且改动要能通过 bump `label_version` 追溯。

**`canada.yaml`**：召回优先，覆盖地名（Canada/Canadian/Ottawa/Toronto/Vancouver/Montréal/Québec/Alberta/…）、机构（CRA/ARC、RCMP/GRC、CSE/CST、Canadian Centre for Cyber Security、Service Canada）、常被攻击/提及的加拿大实体（主要银行、电信、零售）、域名模式（`.gc.ca`、`.ca`）。**必须含法语条目**——Canada 项目的英法双语语料是后续研究价值之一，v0 就不能丢。

**`cyber.yaml`**：与采集共用同一份词表（采集词表即 cyber 判定词表，保持一致）。含英法双语：ransomware/rançongiciel、phishing/hameçonnage、data breach/fuite de données、cybersecurity/cybersécurité 等。

**`topics.yaml`**：固定约 10 个桶，每桶一组关键词，优先级有序，首个命中者胜出，全不命中归 `other`：

```
ransomware / phishing_scam / vulnerability_cve / data_breach /
gov_advisory / critical_infrastructure / fraud_financial /
malware_infostealer / ddos_outage / policy_regulation / other
```

**`is_low_information`**：仅基于可明示的规则（去除 URL 与 mention 后有效 token 数 < 阈值、或纯 emoji/纯转发语）。**[MUST] 只打 flag，不删除。** 一条 "wow this is insane" 脱离上下文无信息，但作为某个 incident thread 的回复，它反映 public reaction，是有价值的。

**`matched_terms` 必须记录命中词**，否则人工无法检查误判。

**验收**：一组手写的 20 条 fixture（含法语、含误判陷阱如 "Canada Goose"、"CRA" 作为其他缩写）断言判定结果；`ccint label --version rules_v1` 幂等。

**不做**：LLM 分类、embedding、命名实体识别。

---

### M5 — Analytics

#### 5a. Descriptive / Pilot 统计

**职责**：输出决定后续架构的那个数字。

```
ccint pilot-stats --label-version rules_v1
```

输出：总采集量、relevant 量、日均 relevant posts/day（按天列出，不只给均值）、unique authors/day、语言分布、topic 分布、unique URL 数、`is_low_information` 占比、以及按 `collection_runs.mode` 拆分的量。

**[MUST] 按天列出而非只给均值**——均值会掩盖 collector 中断和周末效应。

#### 5b. Trend

**职责**：对比两个相邻窗口，产出 candidate trend 排名。

```python
@dataclass(frozen=True)
class TrendCandidate:
    topic_key: str
    cur_posts: int
    prev_posts: int
    cur_authors: int
    prev_authors: int
    abs_delta: int
    rel_growth: float | None     # prev=0 时为 None，不要写成 inf
    cur_share: float             # topic 占当窗口全部 relevant post 的比例
    prev_share: float
    share_delta: float

def detect_trends(
    conn, *,
    label_version: str,
    as_of: datetime,
    window_days: int = 7,
    min_posts: int = 5,
) -> list[TrendCandidate]: ...
```

窗口定义：current = `[as_of - window_days, as_of)`，previous = `[as_of - 2*window_days, as_of - window_days)`。核心 SQL 放 `analytics/sql/trend.sql`。

**[MUST] v0 只有 count 和 unique authors 两个信号**，排序用 `abs_delta` 与 `rel_growth` 的组合规则（简单、透明、可解释即可）。**不要**实现 z-score、moving average、composite trend score、novelty/persistence 权重。理由：没有 baseline 模型（周末效应、平台整体 volume 漂移、单链接 mass repost）时，这些统计量产出的是噪声而非信号，做了反而制造虚假信心。

`min_posts` 阈值的作用是挡掉小样本下的虚假暴涨（2 → 6 不是 200% 增长，是噪声）。

**[MUST] 报告中必须给出 unique_authors**，因为一个人刷 100 条帖子会造成假爆发，而 author 数能立刻暴露它。

**验收**：构造合成数据（某 topic 在 current 窗口翻三倍、某 topic 持平、某 topic 只有单一作者刷量）断言排序与字段计算正确；边界情况 `prev_posts = 0` 不抛异常。

---

### M6 — Agent Analyst

**职责**：接收排名第一的 candidate trend，通过工具调查并产出有证据支撑的解释。

**[MUST] Agent 不判断什么算 trend。** trend 由 M5 的统计层检出，agent 的角色是 analyst 而非 detector。不要给 agent 任何能改变排名或自行决定分析对象的能力。

**三个工具，签名固定**：

```python
def get_topic_stats(topic_key: str, window_days: int, as_of: str) -> dict
def get_representative_posts(topic_key: str, window_days: int, as_of: str, k: int = 10) -> list[dict]
def compare_periods(topic_key: str, as_of: str, window_days: int) -> dict
```

`get_representative_posts` 的选取规则 v0 用确定性方法：按 engagement 排序取 top-k，并强制覆盖至少 3 个不同 author（避免全部来自同一人），排除 `is_low_information`。每条返回必须带 `post_id`、`url`、`published_at`、`author_handle`、`text`。

**模型**：走 Anthropic API（模型 ID 从配置读，不硬编码）。**[OUT OF SCOPE] 不要接本地模型。** 本地 32GB 显存能跑 30B 级量化模型，但在多步 tool use 和"拒绝编造证据"这两点上与前沿模型差距明显，而这恰是本 agent 的全部价值。agent 调用频率极低（只对 top-N 跑），API 成本可忽略。后续用本地模型做对比属于 v1+ 的 ablation。

**Prompt 存文件** `prompts/analyst_v1.md`，版本号写入 `analysis_runs.prompt_version`。

**输出结构** [MUST 为结构化，不是自由文本]：

```json
{
  "topic_key": "...",
  "what_is_happening": "...",
  "is_single_event_or_multiple": "single | multiple | unclear",
  "key_entities": ["..."],
  "evidence": [{"post_id": 123, "why": "..."}],
  "alternative_explanation": "...",
  "confidence": "high | medium | low",
  "confidence_reason": "..."
}
```

**[MUST] `evidence` 中每个 `post_id` 必须真实存在于本次工具返回结果中。** 实现一个校验步骤：agent 返回后逐一核对 post_id，发现幻觉则记入报告的 warning 区，不要静默丢弃。

**[MUST] `alternative_explanation` 是必填字段。** 强制 agent 给出"这个上升也可能只是采集波动/单一新闻扩散"的替代解释，对抗过度确信。

**验收**：用 mock 的工具返回跑一次，断言输出 schema 合法、post_id 校验生效（故意注入一个不存在的 id，断言被捕获）。

---

### M7 — Report & Orchestration

**职责**：串起全流程，写 `analysis_runs`，产出 markdown 报告。

```
ccint analyze --as-of now --window 7 --label-version rules_v1 --top 3
```

流程：`detect_trends` → 取 top-N → 对 rank 1 跑 agent → 渲染报告 → 写 `analysis_runs`（含 trend 快照与报告路径）。

**报告结构**：

1. **Provenance 头部**：`as_of`、窗口、`label_version`、`query_version`、`trend_method`、`agent_model`、`prompt_version`、`analysis_id`。
2. **数据健康**：本窗口内各 `collection_run` 的 status 与数量；**若窗口内存在 `failed`/`partial` run 或存在采集空洞，必须在报告顶部醒目警告**——否则读者会把采集故障读成社会趋势。
3. **窗口是否混入 backfill 数据**的说明。
4. **Top-N candidate trends 表格**：topic、cur/prev posts、cur/prev authors、delta、growth、share change。
5. **Rank 1 的 agent 分析**：含 alternative explanation 与 confidence，证据以可点击链接列出。
6. **局限性固定段落**：单一 source、规则 topic、无 baseline 模型、样本量。这段是模板常量，不由 agent 生成。

**验收**：全流程在空库上给出友好报错而非崩溃；在有数据时产出报告文件，且 `analysis_runs` 有对应记录。

---

### M8 — Health Check

**职责**：`scripts/health_check.sql` + `ccint health`。

输出：各 source 最近一次成功采集时间、最近 7 天每天的 run 数与 status 分布、错误率、最近的 `error_detail`、posts 表最新 `published_at` 与 `collected_at` 的间隔。

**[OUT OF SCOPE] 不要引入 Prometheus，不要引入 Grafana。** 一条 SQL 覆盖 v0 阶段 90% 的监控需求，运维栈属于后期。

---

## 7. CLI 汇总

```
ccint db migrate
ccint collect backfill --since 2026-08-20 --until now
ccint collect incremental
ccint label --version rules_v1 [--all | --unlabeled]
ccint pilot-stats --label-version rules_v1
ccint trend --as-of now --window 7 --label-version rules_v1
ccint analyze --as-of now --window 7 --top 3
ccint health
```

---

## 8. 开发顺序

**[MUST] 严格按此顺序，每步完成后可独立验证再进下一步。不要并行铺开所有模块。**

1. M1 infra → `ccint db migrate` 通过
2. M2 collector → 能从 Bluesky 拉回真实数据并打印（先不入库）
3. M3 ingest → **立即跑一次 backfill，把采集拉起来**
4. **[MUST] 在进入 M4 之前，先配置好 `incremental` 的定时任务并让它开始持续运行。**
   原因：语料随挂钟时间累积，采集停一天就永久少一天数据，而 trend 层需要 baseline。采集必须是最早上线且永不停机的组件，其余模块在数据积累的同时并行补齐。这是整个项目日程上最关键的一条。
5. M4 labeler → `pilot-stats` 能出数
6. M5 trend
7. M6 agent
8. M7 report → v0 完成
9. M8 health check

---

## 9. 明确不做的事（本阶段）

不要因为"顺手"或"以后会用到"而实现以下任何一项：

Grafana / Prometheus / 任何 dashboard；BERTopic / embedding / 聚类；Reddit / X / Mastodon collector；LLM 做相关性分类；命名实体识别；near-duplicate 语义聚类；z-score / 异常检测 / composite trend score；事件中心分析（event-anchored windows）；多窗口尺度（24h/3d/14d 同时保留）；subset / ad-hoc 分析接口；临时数据集导入；本地 LLM 推理；Web UI / API 服务；Kubernetes；ORM；异步框架；用户鉴权。

上述每一项在 v0 之后都有明确的接入位置（见第 1 节五条接缝）。现在实现它们只会让端到端跑通的时间变长，且在 source 和数据量都还没确定的情况下，很可能做成需要推翻的设计。

---

## 10. 质量要求

- **测试**：M2–M6 每个模块至少有单测。外部 API 用录制 fixture，不在测试中打网络。
- **幂等性测试是必须的**，不是可选的：collect、label、analyze 重复执行的行为要有断言。
- **README** 需包含：环境准备、如何起库、如何 backfill、如何配定时任务、如何跑一次完整 analyze、以及 API 实测结论（第 3.2 节要求的那些）。
- **决策记录**：任何与本文档不同的实现选择，在 README 中单列一节说明原因。不要静默偏离。
- **交付时必须报告 pilot 数字**：Bluesky 上 Canada × cyber 的日均 relevant post 数量，以及它的日间分布。这是 v0 最重要的单项产出。

---

## 11. 给实现方的最后说明

如果在实现中发现本文档的某个约束与现实冲突（典型的：Bluesky API 的实际能力与第 3.2 节的假设不符，或 relevant post 数量低到无法支撑窗口对比），**停下来在 README 记录冲突并说明你的处理方式，而不是悄悄改变设计**。第 1 节的五条原则即使在数据量很小的情况下也应当保持——它们保护的是后续扩展的成本，与当前数据规模无关。
