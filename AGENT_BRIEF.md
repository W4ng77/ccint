# ccint — Agent 工作指令

给 Claude Code agent。目标仓库:ccint 实现仓(collector / labeler / analytics / agent / tests),
不是 write-up 仓。

---

## 0. 定位:这是工具,不是论文

ccint 现在的产出是**一份诚实但没有消费者的报告**。它能证明自己没检测到趋势,
这个能力是真实成就,但它不是产品。

本轮改造的目标只有一条:**让这个系统每天产出有人愿意读的东西,同时让"我们采到的样本代表什么"
这个问题有可核验的答案。**

两条判据,贯穿全部改动:

| | 判据 |
|---|---|
| **分析有意思** | 输出单位是**事件**,不是 topic volume。每天能说出"发生了什么、谁在说、引了什么源"。 |
| **采样有保障** | 覆盖率是**测出来的**,不是假设的。每个数字能回答"这是从什么样本里来的、漏了多少"。 |

凡是不服务这两条的改动,不做。

---

## 1. 先调研,不要直接改

我(给你写这份指令的人)**没有读过这个仓库的代码**,下面所有判断基于项目 write-up。
所以**第一步不是写代码**。

先做一次 survey,输出到 `AGENT_SURVEY.md`,内容:

1. `tree -L 3`、各 CLI 子命令的 `--help`
2. `src/ccint/labelers/` 的规则引擎结构:词表在哪、`is_relevant` 的布尔逻辑长什么样、
   `requires_corroboration` 怎么实现、topic 是单值还是集合
3. `src/ccint/analytics/` 里 trend detection / null model / broadcast filter 的接口边界
4. `src/ccint/agent/` 的 prompt 和 tool 定义
5. schema:`posts` / `post_labels` / `collection_runs` / `analysis_runs` 的列
6. `eval/sample.tsv` 的实际列和标注分布
7. **下面每一项任务,标注"指令假设 vs 实际代码"的差异。**

如果某项任务的前提在代码里不成立(例如 topic 其实已经是多标签),**在 survey 里说明并跳过**,
不要为了完成任务清单而强行改。宁可少做一项,不要基于错误前提改动。

survey 完成后再开始第 3 节。

---

## 2. 不可违反的约束

这些是硬约束,任何任务都不得绕过。违反其中任何一条,回滚该改动。

**P1 — 原始数据永不丢弃。** `raw_payload` 逐字保留。新增派生字段可以,原地覆盖不行。
唯一已有例外是 U+0000 剥离,不要新增例外。

**P2 — 标签永不覆写。** 新 labeler 必须作为新的 `label_version` 与 `rules_v2` **并存**,
可逐行对比。不要修改 `rules_v2` 的输出,不要删除 `rules_v1`。

**P3 — 每个数字带 provenance。** 新增的任何分析输出都要记录到 `analysis_runs`,
带 `label_version` / `query_version` / 方法标识 / 时间戳。新增方法要新增标识,不要复用旧的。

**P4 — 时间永远 UTC。** 不要引入任何本地时区依赖。新增的日期分桶沿用现有 `date_trunc` 约定。

**P5 — 系统故障必须与社会趋势可区分。** 新增采集流要同样写 `collection_runs`
(mode / status / 窗口 / 计数 / 错误)。不要新增绕过 run bookkeeping 的采集路径。

**伦理边界(这条特别注意)。** 项目声明:不做去匿名化、不做 profile 聚合、不追踪个人;
author identifier 仅用于检测发帖集中度,是方法学控制而非研究对象。

因此:
- **不要构建作者级地理推断**(根据 profile / handle / 关注图猜作者在哪)。这是 profile aggregation。
- 加拿大相关性只能从**帖子内容的实体**判定,不能从作者身份判定。
- 允许使用的作者级量:发帖数、活跃天数、公开 follower 数、engagement。仅此。

**不要碰的模块:** `ingest`(幂等 upsert、run bookkeeping、stale-run reaping、时间戳修复
——这些都是对的)、`ops`(cron / health check / log rotation)、migrations 的既有内容。

---

## 3. 任务

分四批。每批一个 PR,每批有验收标准。按顺序做,前一批的验收不过不要开始下一批。

---

### 批次 A — 采样保障:让覆盖率可测

这批的目的不是提高覆盖率,是**让覆盖率变成一个有数的量**。现在
"我们漏了多少相关帖"完全不可观测,这是整个系统最大的认知盲区。

**A1. 逐词产出instrumentation**

对 34 个搜索词逐个记录:每次采集返回多少帖、其中多少被判 relevant、
多少是该词**独家**命中(没有其他词也命中)。落到新表或 `collection_runs` 的扩展列。

验收:能输出一张表,每行一个词,列为 `posts_returned / relevant / unique_hits`,
按 relevant 降序。

*为什么:* 这张表直接告诉你词表哪里饱和、哪里该扩。已有工作显示专业团队精心构造的
安全词表之间召回差异超过一半,而且常缺 `phishing` / `cve` / `patch` 这类基础词
——所以词表大概率远未饱和,但**先测再扩**。

**A2. 词表扩容到约 100 个**

基于 A1 的数据扩,不要凭直觉加。新 `query_version`,旧版本保留。

法语**不要再加翻译术语**。项目已测出 `rançongiciel` 7 天返 1 条、`hameçonnage` 5 条,
而 `cybersécurité` 数百——这是已验证结论,法语信号走机构名和地名。

验收:`query_version` 递增;新旧版本的产出可对比;A1 的表在新词表下重跑。

**A3. 新增加拿大侧查询流(这条要读懂再做)**

现状:34 个词零个提加拿大。后果是**任何不使用这 34 个词里任一个的加拿大网安帖,
物理上不可达**。一条"RCMP 说我们系统被入侵"、一条"Alberta Health 数据外泄",
措辞里没那 34 个词就永远看不到。

项目 write-up 论证过"不能把 canada 当搜索词,否则 GTA/CRA 那类错误会永久冻结进 corpus"。
**这个论证只对 AND 过滤成立。**作为**独立的 OR 流追加**,它是严格召回增加,不冻结任何东西
——因为你永远不删查询,只加。

实现:第二条查询流,key 为加拿大机构和地名 + 通用事件词
(RCMP / GRC / CSE / cyber.gc.ca / Canada Revenue Agency / 省级卫生教育机构 / 主要加拿大企业名)。
走同一个 ingest 路径,同一套 run bookkeeping,新 `query_version`。

验收:两条流的产出可分别统计;能回答"有多少相关帖只有加拿大流抓到、cyber 流抓不到"。
**这个数字就是原设计的召回缺口。**

**A4. 不要做 firehose**

如果你想到 Jetstream,答案是不做,理由已经算过:约 24GB/天 × 逐字永久存储 = 与 P1 正面冲突;
为省盘在 ingest 处过滤就是重新造一条不可逆采集边界;Bluesky 官方明说不承诺把 Jetstream
作为长期稳定 API 维护,而本系统的核心原则是采集器绝不能停;而边际相关帖增益有限。

**A5. 协议字段去重**

AT Proto 有显式的 `app.bsky.feed.repost` 和 quote-post 记录。用这些字段做归并,
**不要写相似度计算(SimHash/MinHash)**——协议字段免费拿到大部分收益。

分析单位从 post 改成 cluster。注意方向:**去重会让有效样本变小、统计结果更负。**
这是正确的取舍,不要因为数字变差而回退。

验收:`posts` 与 cluster 的映射可查;能报告去重前后的有效 n。

---

### 批次 B — 预处理重写:规则做宽召回,LLM 做窄判定

现状是 `is_relevant = cyber_terms AND canada_terms`,**两侧都是窄工具且用 AND 连接**,
所以召回上限是两个词表的交集。GTA/CRA/CSIS 那三个错误、86.3% 的精度天花板、
`other` 吃掉 44%,全部是这一个结构决定的。

关键事实:**每月只有约 650 条相关帖,这个量级上 LLM 逐条处理几乎免费。**
参照:SOTA 系统在 44,093 条推文上跑实体抽取耗约 1966 秒,这是他们必须靠关键词
预过滤来规避的瓶颈;按比例你是约 30 秒。**他们必须做的妥协,你不需要做。**

**B1. 规则降级为召回过滤器**

```
# 现在
is_relevant = has_cyber_term AND has_canada_term AND corroborated

# 目标
is_candidate = has_cyber_term OR has_canada_signal OR has_cve_id
```

`requires_corroboration` 整块退役。它解决的是关键词歧义,而这在文献里被记载为
关键词方法的**根本失效模式**(某系统把 "Tropic" 当关键词——来自 Tropic Trooper 黑客组织
——结果捞进讨论 "Tropic Thunder" 的帖;根因是关键词方法无法考虑语义)。
GTA = Grand Theft Auto 是同一个 bug。在这条路上打补丁的天花板就是已测到的 86.3%。

**保留规则代码,不要删。**`rules_v2` 继续作为 baseline 存在。

**B2. LLM 判定层,单次调用出全部标签**

新 `label_version = llm_v1`。schema:

```json
{
  "is_cyber": bool,
  "canada_relation": "content_entity | none",
  "informativeness": "event | commentary | uninformative | non_security",
  "topics": ["data_breach", "vulnerability"],
  "entities": {
    "victim_org": [], "location": [], "malware": [],
    "vulnerability": [], "threat_actor": [], "campaign": []
  }
}
```

四个设计点,每个对应一个已知 bug:

**① `topics` 改多标签。** 已有工作明确指出这些安全类别在实践中不互斥、现实事件常跨多类;
某研究 10,392 条标注里 9.84% 是多标签,并指出强制单标签会让不同标注者把相似的帖
分到不同类别、损害分类性能。

现状每帖一个 topic ⇒ 约 10% 的帖被任意归类,而**这个任意性是噪声直接注入 per-topic 日计数**,
同时抬高 `other`。一条"勒索软件利用漏洞导致数据泄露"的帖有三个合法标签,可能一个都没匹配。

**② 新增 `uninformative` 类。** 同一工作把 non-security(50.87%)和 uninformative(16.08%)
分开,后者是"与网安有关但信息量不足、无法归入具体事件类别",例子:
`"76% of organizations worldwide expect to suffer cyberattack this year"`。

这类帖现在判为 relevant、计入 topic volume。**这是与 GTA 完全不同的精度泄漏**
(GTA 是"不是加拿大网安",这是"是加拿大网安但无内容"),而且很可能是 `other` 那 44% 的
主要成分之一。

**③ `location` 用内容实体,不用作者画像。** 见第 2 节伦理约束。CRA 是 Canada Revenue Agency
还是 EU Cyber Resilience Act,是个实体链接判定,不是词共现判定——这才是 CRA/CSIS 歧义的
正确解法,而且它是 post-level 的,不碰作者身份。

**④ 实体只取上面 6 类。** 参照系统用了 13 个 STIX Domain Object,但那是为百万级图规模服务的。
你的量级 6 类够用,且每类都直接服务于聚类或加拿大判定。

分层调用控制成本:规则宽过滤 → 便宜模型粗筛 relevance → 强模型出全部标签。
目标月成本个位数美元。

验收:
- `llm_v1` 与 `rules_v2` 在同一批 post 上可逐行 diff,输出分歧样本
- 在现有 eval 集上跑出 `llm_v1` 的 precision/recall,与 `rules_v2` 并列
- 能报告 `other` 在 `llm_v1` 下降到多少、`uninformative` 占比多少

**B3. 不要做 BERTopic / embedding 聚类来处理 `other`**

n=287 上 HDBSCAN 会把大部分扔进噪声簇。直接让 LLM 把 `other` 全读一遍提出分类,
一个 prompt、几分钱、更可解释。整个项目的论点就是别在数据撑不住的地方上复杂机械,
这里也适用。

---

### 批次 C — 分析改成事件轨

**C0. 冻结趋势层**

`detect_trends` / null model / 置换检验 / power analysis:**代码全部保留,停止在日常报告里
发布 topic trend 排名。**

它们已经完成使命(证明当前窗口无可检出趋势),是这个项目最好的方法学产出。
但缺口是 8×,而 DOW 分层 baseline、扫 duration、negative binomial 这类改进
乐观收益等效 1.5–2× 数据,**且唯一服务的输出在事件轨上线后就没有消费者了**。
等 corpus 到 4× 再解冻。

**本批不做任何统计方法改进。**

**C1. 外部事件登记表**

新表 `external_events`:`event_id, source, first_published_at, title, cve_ids[],
affected_entity, canada_relevant, url`。

灌入源:`cyber.gc.ca` advisory(RSS)、CISA KEV Catalog(公开 JSON)、NVD CVE 日期、
加拿大主流媒体 RSS(CBC / Globe / La Presse,`canada_relevant` 需人工确认)。
回填对齐现有 corpus 窗口。

**这是整批 ROI 最高的一项:它给系统第一个不是自己标注的参照物。**
现有全部 precision/recall 数字都挂在"写 labeler 的同一个 agent 标注"上,
而外部登记表是外部机构发布的、带时间戳的、你无法影响的。

**C2. 事件聚类**

按共享实体(B2 的 6 类)+ 时间窗聚类。**时间必须是聚类特征,不是事后排序。**

参照系统的消融:去掉时间特征聚类质量 NMI 从 0.7344 降到 0.6968,
噪声点从 43 涨到 98;具体失效案例是同一漏洞的帖被拆成两簇、两个不同事件被并成一簇。
成本为零,不要省。

**不要上 GAT 或图神经网络。**那是为百万级图规模服务的。650 条帖直接按共享实体 + 时间窗
做连通分量或 DBSCAN。

**C3. cluster 级广播过滤(这条你已经有数据了)**

```
Score(C) = #unique_authors(C) / #posts(C)   ≥ 0.80
```

参照系统用的就是这个,阈值 0.80,理由一字不差:剔除被少数用户或单个自动账号
重复发布的内容;实测超过 60% 的簇 score 超过 0.80。

对比现有账号级过滤(`n_posts ≥ 5 ∧ n_active_days ≥ 4 ∧ zero_engagement ≥ 60%`):
需要历史、需要 settling time 护栏、需要跨窗口稳定。**cluster 级比值对单个簇立即可算,
零历史依赖。**

而且 `ccint viz` 的 signature chart 已经同时算了 posts 和 unique authors。
ransomware 是 105 posts / 12 authors → score 0.114,会被阈值瞬间毙掉,
不需要先鉴定那 11 个账号是不是 bot。**分子分母都在手上,只是没把比值当过滤器用。**

账号级过滤保留给冻结的趋势层。

**C4. 两条流分开评估**

| 流 | 评估什么 | 不评估什么 |
|---|---|---|
| 自动 feed(ecrime.ch / ransomlook 等) | 覆盖度:登记表事件命中率 | 早期性 |
| organic | 早期性(lead/lag time)、独家性 | 覆盖度 |

**不要假设 feed 流价值更高。**已有工作发现只有极小一部分社媒 IOC 来自商业厂商账号,
**个人用户才是早期检出和独家 IOC 的主要贡献者**。feed 买的是完整性,organic 买的是早期性。

**C5. 对照登记表的指标,注意分母**

| 指标 | 定义 |
|---|---|
| 绝对事件数 | 30 天内找到的加拿大相关、可引用事件数 |
| lead / lag time | 首条相关帖时间 − 权威源发布时间 |
| event precision | 系统报的事件里真实存在的比例 |
| feed vs organic 首发 | 每个事件的首条来自哪条流 |

**分母必须限制在该源本来覆盖的范围内。**参照系统的做法是把分母限制在匹配该 feed
目标 IoC 类型和威胁类别的范围,以避免分母偏差。拿 CISA KEV(只收已被实际利用的漏洞)
当分母去算数据泄露事件的 recall,数字没有意义。

**预期要先说清:recall 会很低,这不是失败。**参照系统在 5,291,166 条推文、
1,221,332 个账号上拿到 event recall 0.5563;另两个系统 0.2980 和 0.0265。
你有 650 条相关帖。所以**判据用绝对事件数,不用 recall 比例**。

**C6. agent 收窄为三个确定性任务**

聚类 → 与登记表匹配 → 写带引用的摘要。

**硬约束:agent 不产生任何事实性断言。**受害者名称、CVE 编号、日期全部来自 B2 的抽取层
或 `external_events`,不来自生成。已有工作明确指出 LLM 在威胁情报上不可靠;
现有的 `post_id` 机械校验只管"引用存在"——**引用对了但断言是编的,仍然是错。**

保留现有那个允许 agent 说"没有故事"的 prompt 设计,那是全系统最好的设计之一。

**C7. informative user 排序**

```
Score(U) = mean_t( event_posts(U,t) / posts(U,t) ) · log(followers(U) + 1)
```

参照系统用这个找"提供深度分析、常在事件公开前就有分析"的账号,并发现
**高发帖量的用户不一定是有信息量的用户**(纯按安全帖数量排序,榜首是个自动播报 CVE 的 bot
——这独立复现了本项目的 broadcast filter 发现)。

输出一个加拿大网安的 curated follow list。这是分析师真正想要的东西,
且只用公开 follower 数和发帖行为,在伦理边界内。

---

### 批次 D — 评估集与投递

**D1. 评估集重建**

现有 gold set 有两个问题,都要修:

- **独立性**:由写 labeler 的同一个 agent 标注。这不是 gold,是第二个相关的 proxy。
- **覆盖**:99 条全来自 backfill,incremental 未评估。

新抽样要求:
1. **跨 backfill 和 incremental 分层**
2. **按工作日/周末分层**(周末量约为工作日 45%,不分层会系统性偏斜)
3. 目标 100–150 条,由**非本系统作者**标注
4. 用 confidence-driven sampling 选样本:按 labeler 置信度挑最不确定的,
   而非均匀随机。实测自适应抽样比均匀混合常常少用 200–750 条标注即达同等效能。

**D2. PPI 区间估计**

用少量独立标注 + 全部 labeler 输出,构造有效置信区间。PPI 的关键性质是
**对模型正确性或校准不做任何假设**,覆盖率对 proxy 无条件成立——更差的 proxy 给出
更宽的区间,而不是无效的区间。有现成库(GLIDE 等)。

**重要:PPI 不是独立标注的替代品,是它的乘数。**它减少你需要的独立标签数量,
不减少"必须独立"这个要求。garbage gold in, confidently-wrong interval out。

PPI 要求 labeled 与 unlabeled 同分布——所以 D1 的分层抽样是 D2 的**前置条件**,不是可选项。

验收:`21.3/day` 从点估计变成区间。**区间可能很宽,那也是信息,不要调参把它压窄。**

**D3. 投递**

| 做 | 不做 |
|---|---|
| 事件优先的日报,发到一个真实收件箱/频道 | Prometheus / Grafana |
| feed 流新事件即时推送 | Web UI |
| 单页静态 HTML(事件列表 + signature chart + 数据健康状态) | 实时告警栈 |

现状 `reports/*.md` 落盘,等价于没有 consumer。**这是 practical 的字面含义,
技术含量最低,但决定这个系统有没有用。**

---

## 4. 明确不做

| 不做 | 原因 |
|---|---|
| Jetstream / firehose | 与 P1 冲突;24GB/天;官方不承诺稳定性;边际收益有限 |
| 作者级地理推断 | 违反伦理声明(profile aggregation) |
| BERTopic / embedding 聚类 | n=287 不稳定,LLM 读一遍更好 |
| SimHash / MinHash 去重 | 协议字段够用 |
| GAT / GNN 事件嵌入 | 为百万级规模设计的,你不需要 |
| DOW baseline / EBP scan / negative binomial | 收益 1.5–2×,服务于已冻结的输出 |
| ZIP(零膨胀)模型 | 你的零是采样零不是结构零 |
| sentiment / semantic drift / named network analysis | corpus 撑不住,会制造信心而非知识 |
| 删除任何既有 label_version 或统计代码 | P2;而且它们是方法学资产 |

---

## 5. 交付形式

每批一个 PR,标题 `[A]` / `[B]` / `[C]` / `[D]`。每个 PR 带:

1. 改了什么、为什么
2. 验收标准逐条的实际结果(**贴真实输出,不要写"应该可以"**)
3. 新增测试(现有 191 个测试必须全绿;新功能补测试)
4. 与本指令的偏离及理由

最后写一份 `AGENT_REPORT.md`:

- 每批的验收结果
- **指令里哪些前提在代码里不成立**(这个最重要,我没读过代码,一定有)
- 你发现但指令没覆盖的问题
- 下一步建议

---

## 6. 一句话

现在这个系统能证明自己没找到趋势。改造完之后,它应该**每天能说出发生了什么**,
并且**对每个数字都能回答"这是从什么样本里来的"**。

前者是"分析有意思",后者是"采样有保障"。凡是不服务这两条的,别做。
