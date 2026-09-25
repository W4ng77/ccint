# 数据能力审计：interaction 实验

审计时间 2026-09-25。所有数字都是当天直接查库（pgserver 实例 `pgdata/`，库 `ccint`）
或对 API 做只读探测得到的，不是从文档转抄。

**结论先行：** 可靠的 interaction 结构**能够重建**，但**必须通过 API 重采**，
离线数据不够。现有库里只存了种子帖**向上**的指针（reply parent / root）
以及采集时刻的计数快照；**向下**的邻域（种子帖收到的回复、引用、转发）
绝大部分不在库里。三个所需接口经实测都可以通过 `bsky.social` 访问。
因此本文件末尾附了采集扩展方案，**实验本身尚未开始**。

---

## 1. 种子集 \(P_{seed}\) 的精确定义（提议）

| 维度 | 取值 | 依据 |
|---|---|---|
| 相关性判定门 | `post_labels.label_version = 'llm_v2c'`，`is_relevant = true` | `portal/queries.py:13` 标注为「当前判定门」 |
| topic 标注 | `topic_v2` | 同文件的 `TOPIC_VERSION` |
| source | `bluesky` | RSS 源没有交互结构，见 §3 |
| query version | `cyber_v1`（backfill 2,251 + incremental 242） | `collection_runs.query_version`，经 `first_seen_run` 关联 |
| 窗口 | 2026-08-22 00:21 → 2026-09-23 02:23 UTC | 种子帖 `published_at` 的实际范围 |
| 规模 | **2,493** 条种子帖，1,106 个作者 | 另有 9 条相关帖来自 RSS（`sources_v1`），不计入种子集 |

需要决策的版本问题：

- README §1 的 pilot 数字用的是 `rules_v2`（650 条相关帖）；门户和最新的判定门用的是 `llm_v2c`（2,502 条）。
  两者规模差约 4 倍。**种子集必须二选一并固定下来**，本文件提议用 `llm_v2c`，
  同时建议对 `rules_v2` 种子集做一次敏感性分析。
- **`llm_v2c` 的 provenance 链（2026-09-25 查清，更正本文件初版「provenance 缺口」的说法）。**
  `llm_v2c` 共 93,244 行，没有一行缺溯源，但它**由两种流程产生**：
  - 89,044 行由 `scripts/apply_entity_validation.py` 从 `llm_v2b` 派生，
    `matched_terms` 里记有 `derived_from = llm_v2b` 和 `validation = mechanical_v1`；
    prompt 和模型的溯源记录在 `llm_v2b` 的 `label_provenance` 行上；
  - 4,200 行由 `llm_v2_runner` 对新增的 incremental 帖直接写入（2026-09-23 02:41），
    **没有经过 `validate_entities`**。`validate_entities` 的 docstring 把这一步标为 [MUST]，
    但 runner 里没有调用它。其中有 145 条落在种子集里；离线补跑校验后，**有 40 条不再判为 relevant**（28%）。

  也就是说，同一个版本号 `llm_v2c` 底下混着两种判定口径。种子清单用 `provenance_status`
  把这两部分区分开（见 `scripts/build_seed_manifest.py`），数据库中的标签**没有改动**。
- **`llm_v2d`（2026-09-25 派生）**：`python scripts/apply_entity_validation.py --src llm_v2c --dst llm_v2d`，
  对 `llm_v2c` 的全部 93,244 行统一施加 `mechanical_v1` 校验；`llm_v2c` 本身不做任何改动。
  `matched_terms.derived_chain` 保留完整的派生链：`[llm_v2b, llm_v2c]` 共 89,044 行，`[llm_v2c]` 共 4,200 行。
  prompt 和模型的溯源按链首版本查 `label_provenance`。
  结果：bluesky 上判为 relevant 的帖子从 2,493 条降到 **2,453** 条，
  恰好是种子清单里标为 `direct_unvalidated_fail` 的那 40 条；没有任何一条从不相关变为相关。
- `model_id` 记录的是 `"user"`，即 vLLM 的 `--served-model-name`。按 README 的启动命令，
  它对应 `Qwen/Qwen3-4B-Instruct-2507`，但**数据库本身不能证明实际加载的是哪个权重**。
- `topic_v2` 的 2,502 行在 `label_provenance` 中没有记录，这一条尚未查清。

## 2. 现有表与字段

库里的表：`posts`、`post_labels`、`label_provenance`、`collection_runs`、`analysis_runs`、
`author_profiles`、`post_entities`、`post_cves`、`cve_records`、`external_events`、
`external_event_entities`、`eval_sets`、`schema_migrations`。

`posts` 表：共 109,420 条 bluesky + 30 条 cisa_advisories + 15 条 bleepingcomputer。
Discourse 采集器已实现，但**没有配置任何 Discourse 源，库里没有 Discourse 数据**。

Bluesky 的 `raw_payload` 是 `searchPosts` 返回的 `PostView` 原样：顶层有 `uri`、`cid`、`author`、
`record`、`embed`、`replyCount`、`repostCount`、`quoteCount`、`likeCount`、`bookmarkCount`、
`indexedAt`、`labels`、`viewer`，其中 1,215 条带 `threadgate`。
`record` 里有 `reply`（19,291 条）、`embed`、`facets`、`createdAt` 等。

## 3. 字段能力表

图例：✅ 是｜◐ 部分｜— 否

| 字段 / 关系 | 已存储 | 可离线派生 | 需要 API 重采 | 不可得 | 说明 |
|---|:-:|:-:|:-:|:-:|---|
| post id | ✅ | | | | `source_post_id` = AT URI，`UNIQUE(source_key, source_post_id)` |
| author id | ✅ | | | | `author_id` = DID（稳定）；handle 可变，另存 |
| 发布时间 | ✅ | | | | `published_at` 已按 migration 002 用 `indexedAt` 修正客户端时间戳 |
| 采集时间 | ✅ | | | | `collected_at`、`first_seen_run → collection_runs` |
| reply parent（向上） | ✅ | | | | `in_reply_to` = `record.reply.parent.uri`，19,291 条全部一致 |
| reply root（向上） | ✅ | | | | `record.reply.root.uri` 在 `raw_payload` 里 |
| 种子帖的 parent 帖内容 | ◐ | | ✅ | | 221 条种子帖本身是回复，其中 parent 在库的 65 条，root 在库的 71 条 |
| 种子帖收到的回复（深度 1） | ◐ | | ✅ | | 库里 `replyCount` 合计 464，实际在库的只有 112 条（24%），而且是因为命中关键词才进库，**有偏** |
| 回复的回复（深度 2） | ◐ | | ✅ | | 以种子帖为 root 的在库帖子共 127 条，同样有偏 |
| quote（引用帖） | ◐ | ◐ | ✅ | | 被引用方的 URI 在 `record.embed.record` 里（2,333 + 638 条 embed）。种子帖被引用 140 次，在库的只有 14 次 |
| repost 行为者 | — | | ✅ | | `searchPosts` 不返回 repost；Bluesky 的 repost 是 `User → Post`，**没有 Post → Post** |
| repost 时间 | — | | | ✅ | `getRepostedBy` 只返回账号、不返回时间；逐个 repo `listRecords` 在规模上不可行 |
| like 行为者与时间 | — | | ✅ | | `getLikes` 返回账号和 `createdAt`；不属于实质交互，仅作辅助 |
| 计数（reply / repost / quote / like） | ✅ | | ✅ | | **是采集时刻的快照**。采集时滞 P10 = 9 h，中位数 306 h，P90 = 630 h，incremental 帖普遍被低估，需要统一重取 |
| @mention | ✅ | ✅ | | | `record.facets` 中的 `#mention`，全库 2,140 条帖子带 mention → `User → User` |
| thread 或会话 id | | ✅ | | | Bluesky 没有单独的会话 id；用 `reply.root.uri` 作为 thread id |
| threadgate / 隐藏回复 | ◐ | | ✅ | | 20 条种子帖带 threadgate，重采时要记录 `blockedPost` / `notFoundPost` 节点 |
| actor 分型 | ◐ | | | | `author_profiles.actor_v1`（两轴，372 个作者，建在 `rules_v2` 的 650 条相关帖上）。**1,475 / 2,493 条种子帖的作者没有 profile**，需要重跑分型 |
| broadcast 检测 | ✅ | | | | `analytics/broadcast.py` 加上 `actor_v1` 的 function 轴；另有 `fig_funnel` 的启发式 feed 定义（两者口径不同） |
| relevance 标注 | ✅ | | | | 见 §1 |
| topic 标注 | ✅ | | | | `topic_v2`（2,502 条） |
| Post → CVE | ✅ | | | | `post_cves`，其中 365 条挂在种子帖上 |
| CVE → KEV | ✅ | | | | `cve_records.known_exploited`、`kev_date_added` |
| 外部事件 | ✅ | | | | `external_events`：ransomware.live，CA 78 条（全时段）/ 窗口内 27 条 |
| Post → ExternalEvent | — | ◐ | | | **库里没有 link 表**。oracle 命中存在 `reports/oracle_events.json` 文件里；`post_entities` 与 `external_event_entities` 可以离线做实体匹配，但匹配规则需要单独版本化 |
| RSS / Discourse 交互 | — | | | ✅ | RSS 本身没有交互；Discourse 有 `posts_count`，但当前没有配置任何 Discourse 源 |

**不做**的事（遵守规格）：不用文本相似度推断回复或转发关系，不根据时间邻近拼接 thread。

## 4. API 探测

2026-09-25 用现有的 `BlueskyCollector._get`（经 `bsky.social` 代理、沿用现有 session）
对一条种子帖做了 3 次只读请求，全部成功，0 错误：

| 接口 | 结果 |
|---|---|
| `app.bsky.feed.getPostThread`（depth = 6，parentHeight = 10） | OK，返回的回复数与 `replyCount = 3` 一致 |
| `app.bsky.feed.getQuotes` | OK，返回 2 条引用帖（完整 PostView） |
| `app.bsky.feed.getRepostedBy` | OK，返回 2 个账号（无时间） |

说明：这次探测**没有写入 `collection_runs`**，只做了能力验证，也没有入库任何数据。
正式采集必须记录 run（项目原则 P5）。

深度 2 可行：`getPostThread` 的 `depth` 参数原生支持嵌套回复。可能丢失的节点
（已删除、被屏蔽、被 threadgate 限制）会以 `notFoundPost` 或 `blockedPost` 的形式出现，要如实记录，不补全。

## 5. 采集扩展方案（实验开始前需要确认）

**请求量估计：**

- `getPostThread`：2,493 次。对所有种子帖都要取，而不只是取 `replyCount > 0` 的 335 条，
  因为计数快照已经过时，而且还要顺带统一刷新计数。
- `getQuotes`：约 84 次；`getRepostedBy`：约 370 次（需要翻页的另计）。

合计约 3,000 次请求，按现有速率限制处理，大约是小时量级。

**provenance：**

- `collection_runs.mode` 新增 `'neighborhood'`，需要新 migration 放宽 CHECK，做法同 007。
  `query_spec` 记录种子集定义（label_version、窗口、query_version）、depth、接口列表。
- 邻域抓回来的帖子**不直接混入 `posts` 语料**。它们不是按 `cyber_v1` 查询边界采到的，
  混进去会改变 \(S^{post}\) 的分母，也会被判定门的后续批处理当成新语料来标注。

  建议新建两张表：
  1. `neighborhood_snapshots`：`run_id`、种子帖 id、接口名、`fetched_at`，以及**原样保存**的 `raw_payload`（完整 API 响应）；
  2. `interaction_edges`：从快照派生，带 `edge_version`，按追加方式写入，
     字段包括 src、dst、type（reply / quote / repost / mention / author）、`created_at`（可得时）、`snapshot_id`。

  替代方案是把邻域帖写进 `posts`，同时让下游一律按 run mode 过滤。这样改动面更大，
  而且有漏过滤的风险，所以不推荐。

**时间语义：** 重采得到的是截至 `fetched_at` 的交互状态。种子帖的「交互沉淀时长」
从约 2 天到约 34 天不等，RQ3 分析时要么把它作为协变量，要么只取沉淀 ≥ N 天的种子帖。

## 6. 与规格不一致、需要修正的地方

1. **`Post --reposts--> Post` 在 Bluesky 上不存在。** repost 是一条没有文本的 `app.bsky.feed.repost`
   记录，应当建模为 `User --reposts--> Post`，而且没有可用的时间戳。
   因此 \(S^{interaction}_{k,t}\) 的时间维度**只能来自 reply 和 quote**。
2. \(S^{actor}_{k,t}\)（去重作者数）现在就能算，不需要重采；它就是 v0 的 unique authors 信号。
3. RQ4 能用的外部参照只有 ransomware.live（`external_events`）和 CISA KEV（`cve_records`）。
   \(P_{seed}\) 中挂 CVE 的帖子只有 365 条，挂窗口内勒索事件的帖子只有 26 个事件的量级，
   **样本很小，RQ4 的统计功效要预先估算**。
