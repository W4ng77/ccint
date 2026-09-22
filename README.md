# ccint — Canada Cyber Social Intelligence (v0)

持续采集与 Canada 相关的 cybersecurity 讨论，沉淀成带时间戳的历史语料，
在其上做 temporal trend detection，再由 LLM agent 对检出的 trend 做
evidence-grounded 分析。

本仓库是该系统的 **walking skeleton (v0)**：一条最窄的端到端竖切
（`采集 → 存储 → 标注 → 趋势 → agent 分析 → 报告`）。
完整规格见 [HANDOFF.md](HANDOFF.md)；本 README 记录实现细节、实测结论与决策偏离。

---

## 1. Pilot 数字（v0 最重要的单项产出）

> ⚠️ **本节数字已修订。** v0 交付时基于 `rules_v1` 给出的 **27/天 是虚高的** ——
> 后续评估（§10）测出 `rules_v1` 的 `is_relevant` **精度只有 56%**，近一半是假阳性。
> `rules_v2` 修复后精度升至 86%。下表并列两个版本；**21/天 是目前最可信的估计**。
> 两个 version 的 `post_labels` 都保留在库中，可随时并排比较（HANDOFF §1.2）。

> 语料窗口 2026-08-22 → 2026-09-21（30 天，backfill + incremental），`query_version=cyber_v1`

| 指标 | `rules_v1`（v0 交付值） | `rules_v2`（修订后） |
|---|---:|---:|
| 采集总量 | 88,434 | 88,434 |
| `is_cyber` | 66,470 | 66,845 |
| `is_canada` | 1,165 | 840 |
| **`is_relevant`** | **846** (0.96%) | **640** (0.72%) |
| `is_relevant` 精度 | **56.0%** | **86.3%** |
| `is_relevant` 召回 | 85.6% | **91.7%** |
| `other` 占 relevant | 49.8% | 43.9% |

### **Bluesky 上 Canada × cybersecurity ≈ 每天 21 条 relevant post**

```
rules_v2:  640 条 / 30 天 = 21.3/天
rules_v1:  846 条 / 30 天 = 28.2/天   ← 含约 266 条假阳性，不应采信
```

修订后的数字进一步支持 §8 的结论：**下一步应当加信号源（Reddit / X），
而非细化分析粒度。**

（`ccint pilot-stats` 报出的 `min=0` 来自 2026-09-21 那个只有 2.5 小时的残缺当天；
完整天的最小值是 9。）

#### 日间分布有很强的周末效应

| 星期 | 平均 relevant/day |
|---|---:|
| Mon | 33.3 |
| Tue | 28.5 |
| Wed | 34.5 |
| **Thu** | **45.8** |
| Fri | 30.5 |
| **Sat** | **15.8** |
| **Sun** | **15.4** |

工作日与周末相差近 3 倍。这是 HANDOFF §5a 坚持「按天列出而非只给均值」的直接理由，
也是 §5b 禁止在无 baseline 模型时使用 z-score 的直接理由 ——
任何短于 7 天、或两窗口包含不同数量周末的对比，都会被这个效应严重混淆。

#### Topic 分布（relevant）

| topic | posts | authors | posts/author |
|---|---:|---:|---:|
| other | 421 | 269 | 1.6 |
| ransomware | 118 | 20 | **5.9** |
| malware_infostealer | 110 | 93 | 1.2 |
| data_breach | 98 | 87 | 1.1 |
| fraud_financial | 38 | 37 | 1.0 |
| vulnerability_cve | 19 | 18 | 1.1 |
| phishing_scam | 16 | 15 | 1.1 |
| ddos_outage | 14 | 10 | 1.4 |
| policy_regulation | 7 | 7 | 1.0 |
| critical_infrastructure | 4 | 3 | 1.3 |
| gov_advisory | 1 | 1 | 1.0 |

两点需要在解读任何 trend 前知道：

1. **`ransomware` 的 118 条帖只来自 20 个作者**（5.9 posts/author），而其它桶都接近 1.0。
   量最大的 topic 主要由少数账号（多为安全新闻 bot）撑起，不是广泛讨论。
   这正是 §5b 强制报告 `unique_authors` 要防的情形。
   **§11.4 已把这个怀疑坐实并量化**：`ransomware` 14 天内 45 条中有 40 条来自
   5 个自动化情报 feed，剔除后该 topic 直接从趋势排行榜消失。
2. **`other` 占 50%**。topic 词表对半数 relevant 帖没有覆盖，
   桶间对比的代表性因此受限。

#### 语言分布

英语占绝对多数，法语存在但稀薄。实测中法语专业术语几乎无人使用
（`rançongiciel` 七天 1 条、`hameçonnage` 5 条），而 `cybersécurité` 有数百条。
故 `canada.yaml` 的法语侧以地名与机构名承载，而非译名术语。

---

## 2. 环境准备

```bash
cd /home/xinyu/jingrui
uv venv --python 3.11
uv pip install -e ".[dev]"
cp .env.example .env     # 填入 Bluesky 凭据
```

`.env` 需要的键见 [.env.example](.env.example)。**密钥不入仓库**，`.env` 已在 `.gitignore`。

---

## 3. 起库

```bash
ccint db migrate          # 建库 + 建表，幂等
ccint db psql             # 打印连接串
ccint db stop             # 停实例
```

数据库是**用户态 PostgreSQL 16.2**（`pgserver`），走 unix socket，不占端口，
不需要 sudo。数据目录默认 `./pgdata`。见下文「决策记录 D1」。

---

## 4. 采集

```bash
# 一次性 seed 历史语料
ccint collect backfill --since 2026-08-22 --until now

# 持续采集（供定时任务调用）
ccint collect incremental
```

`backfill` **默认按天分块**（`--chunk-days 1`）。这不是可选优化，见「决策记录 D4」。

### 配定时任务

HANDOFF §8 step 4 把「采集永不停机」列为日程上最关键的一条。本机
`systemd --user` 的 `Linger=no`（用户登出即停），故用 cron：

```bash
crontab -e
# 加入：
*/15 * * * * /home/xinyu/jingrui/scripts/collect_incremental.sh
```

日志写到 `logs/incremental.log`。`deploy/` 下另备了 systemd user unit，
若将来开启了 linger 可以改用它。

Rate limit 余量充裕：15 分钟一轮约 60 个请求，而配额是 3000 / 300 秒，
占用不到 1%。

---

## 5. 标注、统计、趋势、报告

```bash
ccint label --version rules_v1            # 只标未标注的
ccint label --version rules_v1 --all      # 换词表后全量重标

ccint pilot-stats --label-version rules_v1
ccint trend --as-of now --window 7 --label-version rules_v1
ccint analyze --as-of now --window 7 --top 3      # 全流程，产出 reports/*.md
ccint health
```

`analyze` 在缺少 `ANTHROPIC_API_KEY` 时会跳过 agent 并照常产出统计报告。

---

## 6. Bluesky API 实测结论（HANDOFF §3.2 要求）

**文档对 API 细节的描述不可信，以下全部为实测。** 测试日期 2026-09-20。

| 项 | 实测结果 |
|---|---|
| 端点 | `POST/GET https://bsky.social/xrpc/app.bsky.feed.searchPosts` |
| 鉴权 | **必须**。未鉴权返回 `401 AuthMissing`。经 `com.atproto.server.createSession` + app password 取 token |
| `limit` 上限 | **100**（`101` → `400 InvalidRequest`） |
| `since`/`until` 格式 | date-only、`...Z`、带毫秒、naive **四种结果完全一致**，均按 UTC 解释 |
| 历史回溯 | 查 2026-06-01～06-08 正常返回且有 cursor，**可回溯 3 个月以上** |
| 分页 | 结果**按时间倒序**，cursor 向历史方向翻页，跨页零重复 |
| `lang` 参数 | 精确过滤，`fr` / `en` 均正常 |
| searchPosts rate limit | `3000 / 300s`（`RateLimit-Policy: 3000;w=300`） |
| **createSession rate limit** | **`10 / 86400s`** |

### 三个必须进设计的坑

**坑 1 — `createSession` 每天只有 10 次。**
若每轮采集重新登录，15 分钟一轮会在 2.5 小时内烧光配额、锁死账号一整天，
而 §8 要求采集永不停机。实现上 session 落盘（`.bsky_session.json`，`0600`）并在
401 时用 `refreshJwt` 续期（`refreshSession` 不消耗该配额，refreshJwt 有效期约 2 个月）。
正常运行下一两个月才需要真正登录一次。

**坑 2 — `limit=100` 实际返回 99 条。**
页面不满是因为其中有被删除/被过滤的帖子。因此
**「返回数 < limit 即到底」是错误的终止条件**，只能以 cursor 缺失为准。
`tests/test_collector.py::test_pagination_stops_only_on_missing_cursor` 锁住了这一点。

**坑 3 — `*.bsky.app` 在本机被出网策略拦截。**
返回的是 HTML 格式的 `403 Request forbidden by administrative rules.`，
而非 XRPC 的 JSON error —— 由此可判定是网络策略而非 Bluesky 拒绝。
`bsky.social`（PDS host）功能等价且可用。

### 为什么直接打 HTTP 而不用官方 `atproto` SDK

退避逻辑需要读取裸的 `RateLimit-Reset` / `RateLimit-Remaining` / `Retry-After`
header 做精确等待，SDK 会把这层封装掉；且 v0 只用两个端点。用 `httpx` 直连。

---

## 7. 决策记录（与 HANDOFF 不同的实现选择）

### D1 — 用 `pgserver` 而非 docker-compose 起 Postgres

HANDOFF §2 要求 docker-compose。目标机器**没有 docker/podman，也没有 sudo**，无法安装。
改用 `pgserver`（PyPI，自带 PostgreSQL 16.2，正好满足 §2 的「PostgreSQL 16」要求），
走 unix socket，不占端口，与宿主机上已有的 5432 实例零冲突，冷启 0.4 秒。
`docker-compose.yml` 仍保留在仓库中供其它机器使用。

### D2 — 强制 session 时区为 UTC，并加断言

新建 cluster 会继承宿主机时区（实测为 `America/Toronto`）。这不只是显示问题：
`date_trunc('day', <timestamptz>)` 的分桶**取决于 session TimeZone**，
不强制 UTC 会让「按天 relevant posts」按本地日界切分，且跨 DST 出现 23/25 小时的桶。
`migrations/001_init.sql` 做 `ALTER DATABASE ... SET timezone='UTC'`，
`db.py` 连接时再以 `-c TimeZone=UTC` 兜底，`db.assert_utc()` 在每次 migrate 后断言。

### D3 — 用 `indexedAt` 修正不可信的 `createdAt`

Bluesky 的 `record.createdAt` 由**客户端自填**，可为任意值。实测 52,085 条语料中
53 条被回填 >2 天（最远到 2016-04），238 条带未来时间戳。
本项目以时间窗口对比为核心，这类时间戳会直接落进错误的窗口，表现为凭空出现的 trend。

处理：`createdAt` 早于 `indexedAt` 超过 2 天（容忍跨 PDS 联邦同步延迟），
或晚于 `indexedAt` 超过 1 小时（容忍客户端时钟偏移）时，改用 `indexedAt`。
两个原始值都保留在 `raw_payload` 中，判定随时可重算。

已采数据由 `migrations/002_fix_client_supplied_timestamps.sql` 就地修正，
**不需要重采** —— 这正是 §1.1「Raw 永不丢弃」保护的场景。

### D4 — backfill 必须按天分块

searchPosts 结果倒序、cursor 向历史翻页，因此一旦触到 `max_pages` 上限，
被截断的永远是**较早的日期**。直接对整个 30 天窗口分页会系统性地少采老数据，
在 trend 层表现为一条虚假的「讨论量上升」。按天分块后每天有独立翻页预算，偏差消除。

附带收益：每个 `(天, term)` 采完即入库，进程 RSS 从 0.5 GB 降到 4 MB，
且中途失败时已采部分不丢。

### D5 — 新增 `reap_stale_runs()`

§M3 要求「任何异常下 `collection_runs` 都必须被收尾」。`open_run` 的
`except BaseException` 覆盖了异常退出，但**覆盖不了 SIGKILL**（实战中发生过）。
故增加启动时的回收器：把 `running` 且超过阈值时长的 run 标记为 `failed`。
`ccint collect` 的两个子命令都会先调用它。

### D6 — 剔除 NUL 字节

PostgreSQL 的 `text` 与 `jsonb` 都无法存储 U+0000，而 Bluesky 的帖子正文里确实出现过
（实测在 63k 条中命中，`UntranslatableCharacter` 打挂了整轮采集）。
`normalize()` 与 `sanitize_payload()` 递归剔除。NUL 不承载语义，
剔除它不构成「丢弃 raw」—— 它本就无法被表示。

### D7 — `collection_runs` 记账走独立连接

§M3 的异常安全要求有一个不显眼的前提：若记账与数据写在同一事务，
数据事务 abort 时 run 记录会一并回滚，正好留下无法收尾的 `running` 行。
`open_run` 因此持有独立的 autocommit 连接。
`tests/test_ingest.py::test_run_bookkeeping_survives_data_transaction_rollback` 锁住了这一点。

### D8 — `canada.yaml` 不收裸 `.ca` 域名

HANDOFF §M4 的词表建议里包含 `.ca` 域名模式。实测中它是粗筛阶段的主要误报源
（大量非加拿大站点使用 `.ca`），召回收益不抵精度损失。保留 `.gc.ca` 与 `canada.ca`。

### D9 — 缩写组大小写敏感 + 要求 cyber 上下文

`CRA` / `ARC` / `CSE` / `CST` / `GRC` 这些缩写歧义极大（`CRA` 在金融语境是信用评级机构）。
`canada.yaml` 的 `org_abbrev` 组设 `case_sensitive: true` 且
`requires_cyber_context: true` —— 只有同帖另有 cyber 命中时才计入。

### D10 — agent 模型默认 `claude-opus-5`

§M6 只要求「模型 ID 从配置读，不硬编码」。默认值取 `claude-opus-5`。
agent 调用频率极低（只对 rank 1 跑一次），成本可忽略，而本 agent 的全部价值
恰在多步 tool use 与「拒绝编造证据」，不宜为省钱降级。

---

## 8. 与 HANDOFF 的冲突：数据量

§11 要求发现文档约束与现实冲突时停下来记录，而不是悄悄改设计。**这里有一条。**

每天约 27 条 relevant post，7 天窗口约 190 条，摊到 11 个 topic 桶上，
扣掉占 50% 的 `other` 后，实际能过 `min_posts=5` 的桶只有 4–5 个：

```
$ ccint trend --as-of 2026-09-21 --window 7
topic                        cur  prev     Δ   growth  cur_au  prev_au   share Δ
data_breach                   28    14   +14    +100%      24       13    +0.075
other                        113   103   +10     +10%      92       79    +0.048
vulnerability_cve              6     4    +2     +50%       6        4    +0.011
malware_infostealer            8    13    -5     -38%       7       13    -0.028
ransomware                    18    34   -16     -47%       7        8    -0.089
```

**处理方式：不改设计、不改默认值。** §1 的五条原则在数据量小时同样保持 ——
它们保护的是后续扩展的成本，与当前数据规模无关。具体做法：

- `--window` 保持默认 7，但作为**显式可选项**提供 `--window 14`（候选数从 5 增到 8）。
- 报告的「局限性」固定段落里写明样本量问题。
- 这个数字本身就是 v0 要回答的问题。它指向的结论是：
  **下一步应优先加信号源，而不是改分析粒度。**

---

## 9. 测试

```bash
.venv/bin/python -m pytest -q
```

120 个测试。外部 API 用录制 fixture（`tests/fixtures/`），**测试中不打网络**。
DB 测试用独立的 `ccint_test` 库，与业务库隔离。

幂等性测试是硬要求，不是可选：

- `test_collection_idempotent` — 同窗口连跑两次，第二次 `n_inserted=0`
- `test_run_marked_failed_on_exception` — 异常下不留 `running`
- `test_reap_stale_running_runs` — SIGKILL 遗留的 run 被回收
- `test_hallucinated_post_id_is_caught` — 注入不存在的 post_id，断言被捕获且不静默丢弃
- `test_pagination_stops_only_on_missing_cursor` — 短页不得提前终止

---

## 10. 标注器评估（v1 Phase 0）

> v0 交付时 **零 ground truth** —— 没人知道 `rules_v1` 的召回与精度。
> 没有这个数字，改词表就是拍脑袋：加一个词可能提升召回 5%，也可能把 77 条
> GTA 游戏泄露新闻灌进语料，而两者在没有标注集时看起来一模一样。

### 10.1 方法

分层抽样（`relevant` / `cyber_only` / `canada_only` / `neither` 四象限各 25 条，
共 99 条完成标注），按各层在总体中的真实大小加权还原，Wilson 95% 区间。

```bash
ccint eval sample --per-stratum 25 --out eval/sample.tsv   # 导出待标注 TSV
# 填写 TRUE_cyber / TRUE_canada 两列（y/n）
ccint eval score --label-version rules_v1 --compare rules_v2
```

**[MUST] 标注集与 label_version 解耦**：ground truth 存在独立文件，任何新 version
都能在同一批人工标注上重新计分 —— 换词表 = 重新计分，不是重新标注。

⚠️ `eval/sample.tsv` 当前标注由 Claude 完成（first pass，非领域专家）。
它是可覆盖的：改 `TRUE_*` 列后重跑 `eval score` 即可。

### 10.2 结果

| label_version | cyber P/R | canada P/R | **relevant P/R/F1** |
|---|---|---|---|
| `rules_v1` | 87.9% / 88.1% | **64.1%** / 100% | **56.0% / 85.6% / 67.7%** |
| `rules_v2` | 88.7% / 94.8% | **93.7%** / 95.2% | **86.3% / 91.7% / 88.9%** |

瓶颈在 `is_canada` 的**精度**，不在召回 —— v1 的 canada 召回已经是 100%。

### 10.3 rules_v2 修了什么

全部来自对 v1 误判的实测归因，不是猜测：

| 问题 | 实测规模 | 修法 |
|---|---|---|
| **`GTA`** = Grand Theft Auto | **153/846 (18.1%)** 的 relevant 仅靠它命中；`malware_infostealer` 桶 110 条里 **92 条 (84%)** 是「GTA 6 假 ISO 带病毒」 | 移除裸 `GTA`，改用 `greater toronto area`；加游戏名负向守卫 |
| **歧义缩写** | **116/751 (15.4%)** 仅靠缩写认定 Canada。`CRA` = EU **Cyber Resilience Act**，`GRC` = Governance/Risk/Compliance，`CSIS` = 美国智库（csis.org），`CST` = 时区 | 拆成 `org_abbrev_strong`（仅 RCMP）与 `org_abbrev_weak`（需加拿大侧佐证） |
| cyber 召回缺口 | `canada ∧ ¬cyber` 的 319 条中：`dark web` 28 条（含 FBI/1.53 亿驾照事件本身）、`cybercrime` 14 条、HIBP 式 `New breach:` 等 | 补 18 个词条（词频富集挖掘 + 逐条人工核验） |
| 裸 `hack` 名词形式 | `drivers licence hack` 类漏网 | 用「<资产> hack」限定搭配，避开 life hack / growth hack |
| topic 路由缺失 | 新召回的 58 条有 56 条落进 `other` | `topics_v2` 给 data_breach / fraud_financial / gov_advisory / policy_regulation 补词 |

**未采纳的候选**（精度不可接受，已记录在 `lexicons_v2/cyber.yaml` 注释里）：
裸 `leak/stolen/exposed`（77 条中大量是 GTA VI 游戏泄露）、
裸 `CVE`（命中 Countering Violent Extremism）、裸 `class action`。

**关键教训**：`requires_cyber_context` 对 `CRA`/`GRC` **完全无效**，因为它们的歧义含义
**本身就是网安术语**。歧义缩写需要的是**同侧佐证**，不是异侧上下文。

### 10.4 遗留问题：`other` 仍占 43.9%

v2 把 `other` 从 49.8% 降到 43.9%，但仍接近一半。在一个近半数未分类的变量上
做精密统计，精度是假的。这是下一轮最值得投入的地方。

---

## 11. 第一波趋势分析（v1 Phase 2 + 3）

**结论：本窗口没有任何统计上可检出的趋势。这是一个真实结论，不是失败。**

报告：[`reports/wave1_all.md`](reports/wave1_all.md)（全量）、
[`reports/wave1_no_broadcast.md`](reports/wave1_no_broadcast.md)（剔除广播号）。

```bash
ccint analyze --label-version rules_v2 --window 7 --top 5 \
              --null-iter 5000 --null-unit author \
              --exclude-broadcast \
              --agent-backend local --local-model user
```

### 11.1 零模型：排名第一的「+56%」是噪声

v0 的 `detect_trends` 只回答「哪个 topic 变化最大」，不回答「这个变化是否超出随机
波动」。新增 `analytics/nullmodel.py` 用置换检验补上这一层。

| topic | Δ | growth | 零分布 sd | p (FWER) | 判定 |
|---|---:|---:|---:|---:|---|
| `data_breach` | +10 | +56% | 7.45 | 0.718 | noise |
| `other` | +4 | +5% | 13.84 | 0.991 | noise |
| `malware_infostealer` | +3 | +100% | 3.29 | 0.998 | noise |
| `fraud_financial` | +2 | +22% | 4.69 | 1.000 | noise |
| `ransomware` | -5 | -20% | 7.47 | 0.969 | noise |

**零模型下，光靠随机波动就能在某个 topic 上看到 ±28 的变化。** 观测到的最大变化
是 +10。10 个 topic 全部落在噪声带内。

两种零模型（都在 `nullmodel.py` 中实现）：

- `unit=post` —— 逐条重排窗口归属。忽略作者聚集，p 值偏乐观，仅作对照。
- `unit=author`（默认）—— 每个作者的**全部** post 在 14 天跨度上整体循环平移整数天，
  保留其发帖数、topic 组成与 burst 结构，只破坏「作者活动 ↔ 日历时间」的对齐。
  排除的是「某主题的上升只是少数高产作者恰好扎堆」这一解释。

实测 author 单位的零分布比 post 单位宽约 20%（`other`: sd 8.52 → 13.84），
证实语料确实是 over-dispersed 的 —— 这正是 z-score 在此不可用的原因。

**多重比较已校正。** 我们在 10 个 topic 里挑最大的那个来讲故事，是典型的
look-elsewhere effect。每次置换额外记录全 topic 的最大 |Δ|，用 max-statistic 法
给出 `p_fwer`。只有 FWER ≤ 0.05 才可下显著结论。

### 11.2 为什么不用 TREND_AGENT.md §4 要求的 z-score

实测否决：真实语料 max|z| ∈ [0.2, 0.9]，而**纯 Poisson 噪声**的 |z| 中位数就有
1.4~1.7、95 分位 3.0~4.0。z-score 给噪声打的分比给真实信号还高 —— 在这个数据量下
它是**反信息**的。根因是 Poisson 方差假设不成立（一个作者连发 8 条、一条新闻被集中转发）。

### 11.3 要多少数据才够：经验功效分析

把作者池复制 k 份重跑置换检验，实测零分布如何随规模增长（不假设 √k 标度）：

| scale k | relevant/窗口 | 零分布 max\|Δ\| p95 | 同比例放大后的 Δ | 可检出 |
|---:|---:|---:|---:|:---:|
| 1 | 158 | 28.0 | 10 | ❌ |
| 2 | 316 | 40.0 | 20 | ❌ |
| 4 | 632 | 56.0 | 40 | ❌ |
| **8** | **1,264** | **78.0** | **80** | **✅** |
| 16 | 2,528 | 108.0 | 160 | ✅ |

实测标度与 √k 吻合。要让当前这个量级的效应变得可检出，需要约 **8 倍**的数据：

- 按当前 ~22.6 relevant/天，7 天窗口 → 需要 **56 天窗口**（总跨度 112 天，约 4 个月）；
- 或者把采集量提高 8 倍（更多搜索词 / 更多平台）。

**这是 HANDOFF §8「数据量冲突」的定量版本**：v0 当时只能说「数据可能不够」，
现在能说「差 8 倍，且这是 √n 问题，靠调参解决不了」。

### 11.4 广播型账号：24.3% 的语料没有受众

`analytics/broadcast.py`。**3.0% 的账号（11/372）贡献了 24.3% 的 relevant 语料
（158/650），平均互动量是其余账号的 1/27（0.20 vs 5.34）。**

| handle | 条数 | 活跃天 | 零互动率 | 平均互动 |
|---|---:|---:|---:|---:|
| `ecrime.ch` | 31 | 18 | 61% | 0.35 |
| `bsidesedmonton.bsky.social` | 30 | 18 | 69% | 0.30 |
| `cyberintelligence.bsky.social` | 28 | 18 | 100% | 0.00 |
| `hendryadrian.bsky.social` | 18 | 12 | 89% | 0.11 |
| `falconfeedsio.bsky.social` | 15 | 11 | 67% | 0.40 |
| `hackfest.bsky.social` | 9 | 4 | 67% | 0.33 |
| ...（共 11 个） | | | | |

实际构成是两类：**自动化威胁情报 feed**（勒索软件泄露站监控机器人，每出现一个新
受害者就自动发一条）与**安全会议宣传号**（反复推送 CFP 与活动公告）。

最直接的后果：**`ransomware` 这个 topic 14 天内 45 条里有 40 条来自 5 个 feed
（posts/author = 4.5，其余 topic 都在 1.0~1.4）。剔除后该 topic 直接从排行榜消失。**
也就是说 v0 报告里 `ransomware` 的任何「上升／下降」，度量的都是机器人的运行状态。

判定是**行为定义**，不是 handle 名单（名单会腐烂，且不可复现到别的时间窗）：

```
n_posts >= 5  且  n_days >= 4  且  已沉淀帖子的零互动率 >= 60%
```

**[MUST] 零互动率必须控制采集时滞。** `like_count` 是采集瞬间的快照；
incremental 在发帖后约 4h 就抓走，backfill 平均滞后 354h。不加守卫的话，
「零互动」会把所有**刚采到的帖子**误判成广播号。因此只统计
`collected_at - published_at >= 24h` 的帖子。

实测该混杂并不驱动结论 —— 广播号的采集滞后反而**更长**（359h vs 323h），
只看滞后 >168h 的帖子，零互动率仍是 76.7% vs 50.0%、均赞 0.19 vs 3.15。
但守卫必须留着，否则换一批数据就会翻车。

### 11.5 Agent 的两分判定

零模型判定为 noise 时，`analyze` 自动把 prompt 从 `analyst_v1` 切到 `analyst_v2`
（见 `cli.py:_pick_prompt`）。原因：v1 的开头写着「统计层已判定这是 top candidate
trend，你的任务是解释它」—— 在一个不可区分于噪声的变化上，这句话本身就在诱导
agent 编故事。v2 把任务改成「抛开 volume，这些帖子里到底有没有一件事」，
并明确允许「没有」作为正确答案。

v2 强制 agent 就两个**独立**问题表态：

| 字段 | 本次结论 |
|---|---|
| `trend_claim_supported` | `not_supported` —— 量的上升不成立 |
| `coherence` | `one_story` —— 但内容确实是同一件事 |

**这两行不矛盾**，而且正是我们想要的输出形态：帖子里确实有一件具体的事
（Alberta 选民名单泄露），但它在本窗口造成的量级变化与随机波动不可区分。
**可以报道这件事，不能报道「它在升温」。**

本次 agent 由本地 `Qwen/Qwen3-4B-Instruct-2507` 跑出，引用的 5 个 `post_id`
全部通过核验，零幻觉。它还主动指出其中 2 条（驾照泄露）与主叙事无关，
可能是同一 topic 标签下的不同事件 —— 这是 `coherence` 字段想捕捉的东西。

---

## 12. 主题分类：从规则换到 LLM（v1 Phase 5）

**`other` 不是一个桶，是三个性质不同的问题被混在一起。**

对 `other` 做了 120 条开放式编码（[`eval/other_sample.tsv`](eval/other_sample.tsv)，
按 `md5(post_id || 'ccint-other-2026')` 确定性抽样）：

| | 占比 | 外推到 287 条 | 性质 |
|---|---:|---:|---|
| **MISS** 分类器召回失败 | **30%** | ~86 | 本该落在**已有**桶里 —— 是 bug，不是缺口 |
| **NEW** taxonomy 缺口 | **56%** | ~160 | 现有 11 个桶确实没有位置 |
| **FP** 相关性误判 | **12%** | ~33 | 根本不是 Canada×cyber |
| GENERIC | 3% | ~7 | 相关但无可归纳主题 |

### 12.1 漏检是成批的，不是零散的

`other` 里 **19% 本该是 `data_breach`**，而且集中在同一批事件上：

| 事件 | 落在 `other` 里的条数 |
|---|---:|
| Thomson Reuters / 安省法院 C-Track 泄露 | **13** |
| 1.53 亿美加驾照泄露 | **7** |
| Golf Canada 56.9 万条 | 2 |
| SickKids 员工数据 | 1 |

**原因不在词表内容，在匹配机制。** 规则匹配的是术语（`data breach`、
`records exposed`），而新闻写的是自然语言 —— *"court records were accessed"*、
*"the case management system was hacked"*、*"personal information may have been
stolen"*。继续打词表补丁追不上这个。

### 12.2 规则版还有第二种病：把关键词当主题

第二轮验证（[`eval/migration_sample.tsv`](eval/migration_sample.tsv)，60 条
**非 `other` 来源**的分歧）暴露了反方向的错误 —— `rules_v2` 会因为关键词
出现在无关语境里而误分类：

- 会议议程标题 *"Confessions of a Critical Infrastructure Intern"* → `critical_infrastructure`
- 招聘启事 *"hiring a Vulnerability Management Specialist"* → `vulnerability_cve`
- RustConf 演讲预告 *"Reverse Engineering Rust Malware"* → `malware_infostealer`
- 政治吐槽里的 *"spyware"* → `malware_infostealer`
- 链接 URL 里的 `cve-2026-10053` → `vulnerability_cve`

也就是说 `rules_v2` **召回和精度同时有问题**，而且两种错误方向相反。

### 12.3 `llm_v1`：只换机制，不换别的

`src/ccint/labelers/llm_v1.py`，本地 `Qwen/Qwen3-4B-Instruct-2507`，
guided decoding，temperature 0 + 固定 seed。

```bash
ccint label-llm --all --workers 8     # 650 条 / 16 分钟 / 零 API 成本
python scripts/eval_llm_topics.py     # 对照人工编码评估
python scripts/compare_label_versions.py rules_v2 llm_v1
```

**[MUST] 只重判 `topic_key`。** `is_cyber` / `is_canada` / `is_relevant`
原样继承 `rules_v2`，且只处理它判为 relevant 的 650 条 ——
一次只动一个变量，否则两个 version 的差异无法归因。
模型对「这根本不是 Canada×cyber」的意见记进 `matched_terms.off_topic`
作为旁证，**不改写 `is_relevant`**。

### 12.4 两轮验证的结果

**第一轮（`other` 样本，120 条）**——召回与克制必须一起看，
只看召回会奖励「什么都往桶里塞」的模型：

| | 初版 prompt | 收紧后 |
|---|---:|---:|
| **召回**：规则漏检的 36 条中归对桶 | 92% | **89%** |
| 其中 `data_breach` | 23/23 | **23/23** |
| **克制**：taxonomy 确无对应的 67 条中留在 `other` | 79% | **96%** |
| 调和平均 | 0.85 | **0.921** |

初版把 13 条硬塞进了 `policy_regulation` —— 因为 prompt 里写了
"data-sovereignty rules"，把加欧联盟、北极、国防采购全吸了进去。
收紧该桶的定义并加上显式反向约束后降到 2 条。
代价是 `policy_regulation` 召回从 5/6 降到 4/6，是可理解的取舍。

**第二轮（分歧样本，60 条非 `other` 来源）**：

| verdict | 条数 |
|---|---:|
| `llm_v1` 更正确 | **55 (92%)** |
| `rules_v2` 更正确 | 2 |
| 都说得通（新闻摘要多主题） | 2 |
| 都不对 | 1 |

在 57 条有明确优劣的分歧中，**`llm_v1` 正确率 96%**。

> ⚠️ **但这个数字被事件集中度放大了。** 60 条里有 **36 条（60%）来自
> 同一条新闻**（1.53 亿驾照泄露）。全语料 `fraud_financial → data_breach`
> 的 67 条迁移，绝大多数也是这一条。所以这**不是 67 次独立的纠正，
> 是一次纠正 × 67 条报道**。

### 12.5 全语料迁移

650 条中 **215 条（33%）判定不同**，`other` 占比 **44.2% → 31.4%**：

| topic | `rules_v2` | `llm_v1` | Δ |
|---|---:|---:|---:|
| `data_breach` | 115 | **266** | **+131%** |
| `other` | 287 | 204 | −29% |
| `fraud_financial` | 85 | 19 | −78% |
| `malware_infostealer` | 14 | 3 | −79% |
| `vulnerability_cve` | 8 | **0** | −100% |
| `policy_regulation` | 6 | 17 | +183% |

`vulnerability_cve` 归零值得单独说明：该桶原有的 8 条**全部**是误匹配
（招聘启事、URL 里的 CVE 编号、地缘政治评论）。本语料 30 天内
**没有一条真正讨论具体漏洞的加拿大相关帖子**。

### 12.6 对趋势结论的影响：没变，但百分比塌了

```
rules_v2:  data_breach  28 vs 18  = +56%   p_fwer 0.718  noise
llm_v1:    data_breach  55 vs 45  = +22%   p_fwer 0.734  noise
```

**结论不变（仍是 noise），但那个抢眼的 +56% 塌成了 +22%。**

原因是修复召回让**两个窗口的计数都差不多翻倍**，`abs_delta` 仍是 +10，
而分母从 18 涨到 45。换句话说：**原来的「+56%」有相当一部分是小分母造成的
视觉效果，而不是真实的增长幅度。**

这反过来支持了 §11 的结论：在这个数据量下，相对增长率是不可靠的叙事工具，
`abs_delta` 配合零模型才是。

### 12.7 踩到的坑：guided decoding 的空白死循环

vLLM 的 `response_format: json_schema` **不约束 JSON 之间的空白**。4B 模型
写完一段长 `rationale` 后会陷入 `\r\n` 死循环，直到耗尽 `max_tokens` ——
对象永远收不了尾，`json.loads` 必然失败（实测 120 条里 2 条）。

两处修复，缺一不可：

1. **字段顺序**：枚举/布尔在前，自由文本 `rationale` 殿后。即使尾部烂掉，
   需要的信息也已经完整产出。
2. **容错解析**：`parse_verdict_json()` 正常解析失败时逐字段抢救。
   **但 `topic_key` 抢救不出来时必须报错** —— 猜一个主题会静默污染整个
   `label_version`，事后无法分辨哪些是猜的。

---

## 13. Actor type：把「广播号」拆成两个正交的轴（v1 Phase 6）

**`is_broadcast` 这个布尔值是错的**：它把两件不相干的事压成了一个判断。

```
ecrime.ch / ransomlook       每条都在报一个真实受害者 → 事件 ground truth
bsidesedmonton / hackfest    反复推自己的活动         → 生态指标
```

它们的共同点**只有「没人点赞」**。旧口径把它们一起剔除，等于把语料里唯一
系统性的事件流也丢掉了。

### 13.1 轴 1：audience（实测）

已沉淀帖子（采集时滞 ≥24h）的平均互动 ≥ 1.0 即 `engaged`。
**[MUST] 无沉淀帖时判 `unknown`，不判 `unheard`** —— incremental 在发帖后
约 4h 就抓走，那时互动必然是 0；「还没来得及被看见」与「没人看」是两回事。

### 13.2 轴 2：function（内容判定，**不是**行为推断）

`src/ccint/analytics/actor_llm.py`。读该账号的帖子判断它在做什么：
`incident_feed` / `news_media` / `promotion` / `individual` / `org_other`。

**这里刻意不做「是不是机器人」的行为推断，因为实测不可靠：**

| 特征 | 已知 bot | 已知会议号 | 为什么不能用 |
|---|---|---|---|
| 发帖时段熵 | 0.67–0.74（全天候） | 0.38–0.52（工作时间） | n=5 时熵的上限只有 log(5)/log(24)=0.506，`ransomlook`（真 bot）因此只测到 0.42。本语料 **285/372 的作者只发过 1 条** |
| 域名集中度 | 100% | 56–67% | `journodale`（真人记者，只转自己供稿的媒体）也是 100% |
| 文本模板化 | 0.07–0.66 | 0.13–0.23 | `journodale` 是 0.83，比多数 bot 还高 |

**从行为推断「是不是机器」在这个样本量下做不到。** 可靠的是读内容判断
**这个账号在做什么** —— 而那恰好也是研究上更有意义的问题。

### 13.3 一个必须加的下限：模式类别需要足够样本

首轮跑出 61 个 `incident_feed` 作者，远多于预期。逐条查：**只发过 1 条的
38 个里，只有 `haveibeenpwned.com` 是真 feed**，其余全是转发驾照泄露新闻的
普通人（`elainebg`、`matthewbennell`、`saucetweet`…）。

`incident_feed` 与 `promotion` 描述的是**反复出现的行为**，一两条帖子在
定义上就确立不了模式。加下限 `n_posts >= 3` 后：

```
incident_feed 作者   61 → 10        intel_feed_raw   156 条 → 112 条
promotion 作者       22 →  4
```

这不只是可靠性修正，**也是语义修正**：在本语料里只出现 1 条的账号，
无论它在别处是什么，**在这里都不构成一个 feed**。

画像按样本量分三层，下游可据此筛选：

| tier | 作者 | 帖子 | 实测可靠性 |
|---|---:|---:|---|
| `established` (≥5 条) | 16 | 196 (30%) | function 判定**全部正确** |
| `provisional` (3–4 条) | 21 | 69 (11%) | 可用 |
| `single` (1–2 条) | 335 | 385 (59%) | 只能判「这条像什么」 |

### 13.4 结果

| actor_type | 作者 | 帖子 | 占比 |
|---|---:|---:|---:|
| `intel_feed_raw` | 9 | 112 | **17.2%** |
| `unclassified` | 79 | 97 | 14.9% |
| `news_with_reach` | 71 | 89 | 13.7% |
| `news_syndicated` | 65 | 88 | 13.5% |
| `org_reaching` | 41 | 72 | 11.1% |
| `org_unheard` | 41 | 63 | 9.7% |
| `promotion_unheard` | 3 | 43 | 6.6% |
| `individual_unheard` | 27 | 37 | 5.7% |
| **`organic_attention`** | 34 | 35 | **5.4%** |
| `promotion_reaching` | 1 | 11 | 1.7% |
| `intel_feed_amplified` | 1 | 3 | 0.5% |

**只有 5.4% 的语料是「一个人在说话且有人回应」。** 这个数字本身就是一个结论：
Bluesky 上的 Canada×cyber 语料主要由**新闻转述**（27%）、**情报 feed**（18%）
和**机构账号**（21%）构成，个人讨论是少数。

### 13.5 回报：不同的问题用不同的过滤器

这是二维拆分的全部意义。旧的 `is_broadcast` 只能回答「剔不剔」：

```bash
ccint trend --actor-mode attention   # 剔除情报 feed 与宣传 —— 度量社会注意力
ccint trend --actor-mode events      # 保留情报 feed        —— 度量事件覆盖
ccint trend --actor-mode ecosystem   # 只看宣传与机构       —— 度量社群生态
ccint trend --actor-mode all         # 不剔除
```

实测差异：

| | `all` | `attention` | `events` |
|---|---|---|---|
| `ransomware` | 19 vs 24 在榜 | **完全消失** | 19 vs 24 在榜 |

**同一份数据，两个都成立的不同故事**：没有人在*讨论*勒索软件，但勒索事件
确实在被*报告*。旧口径无法同时说出这两句话。

**[MUST] `unclassified` 在任何 mode 下都不排除。**「判不了」不等于
「判为无关」；把不确定当阳性会系统性压低低产账号的语料，而低产账号正是
最像有机讨论的一批 —— 压低的方向与结论方向一致，是最危险的一类偏差。

### 13.6 对趋势结论的影响

```
mode=all        cur=152 prev=139  噪声带 ±26   data_breach Δ=+10  p_fwer 0.733  noise
mode=attention  cur=124 prev= 94  噪声带 ±23   other       Δ=+16  p_fwer 0.232  noise
mode=events     cur=146 prev=117  噪声带 ±23   other       Δ=+17  p_fwer 0.214  noise
```

**三种口径下都仍是 noise。** 但 `attention` 口径下排名第一的变成了 `other`
（+46%），而 `data_breach` 的 `share_delta` 由正转负 —— 说明
`data_breach` 的相对上升有一部分是情报 feed 贡献的，不是讨论的升温。

---

## 14. 本阶段明确没做的事

按 HANDOFF §9，以下均未实现，且在代码里没有预留位：
Grafana / Prometheus / dashboard；BERTopic / embedding / 聚类；Reddit / X / Mastodon
collector；LLM 做相关性分类；NER；near-duplicate 语义聚类；z-score / 异常检测 /
composite trend score；事件中心分析；多窗口尺度；本地 LLM 推理；Web UI / API 服务；
Kubernetes；ORM；异步框架；用户鉴权。

v0 也不使用 GPU：规则标注器不需要模型，agent 走 API，无任何 CUDA 依赖。
