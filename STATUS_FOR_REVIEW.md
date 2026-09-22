# ccint 现状说明(供外部讨论)

写给一个没读过这个仓库的人。所有数字都直接写在文中,不需要查代码。
截至 2026-09-22。

---

## 1. 这个项目是什么

**ccint (Canada Cyber Social Intelligence)** —— 一个纵向社交媒体情报系统:
持续采集 Bluesky 上与加拿大相关的网络安全讨论,检测随时间的变化,
并让一个 LLM agent 产出有证据支撑的分析。

研究问题原本是:**加拿大网安讨论的主题结构随时间怎么变?**

做到现在,这个问题的答案是「在当前采样规模上测不出来」,而这个**测不出来**
本身是经过统计检验的结论,不是没做完。详见 §4。

---

## 2. 数据现状(全部实测)

**语料窗口:** 2026-08-22 → 2026-09-22(30 天)

| 量 | 值 |
|---|---|
| 采集总帖数 | 91,551 |
| `rules_v2` 已标注 | 90,364 |
| 其中判为 relevant | **650**(0.72%) |
| 判为 irrelevant | 89,714 |
| 完全未标注(新采集,标注滞后) | 1,187 |
| 独立作者(relevant 内) | 372 |

**采集配置:** 34 个搜索词(25 英 + 9 法),**零个词提加拿大**。
每个词单独发一次 Bluesky `searchPosts`,结果合并去重。
backfill 成功 4 次失败 2 次;incremental 成功 63 次、partial 42 次
(partial 全部来自一次 token 失效)。

**主题分布(`llm_v1` 标注,n=650):**

| topic | posts | 独立作者 |
|---|---:|---:|
| data_breach | 266 | 202 |
| **other** | **204 (31.4%)** | 116 |
| ransomware | 101 | **13** |
| fraud_financial | 19 | 18 |
| policy_regulation | 17 | 16 |
| phishing_scam | 15 | 14 |
| ddos_outage | 12 | 8 |
| gov_advisory | 8 | 8 |
| critical_infrastructure | 5 | 5 |
| malware_infostealer | 3 | 2 |

注意 `ransomware` 一行:101 条帖子只有 13 个作者。这不是一个话题,
是几个自动情报 feed 的输出。

---

## 3. 实现架构

### 3.1 管线

```
Bluesky searchPosts(34 词 × 每词一次)
        │  幂等 upsert,raw_payload 逐字保留
        ▼
   posts ──────────────────────────────────────────┐
        │                                          │
        ├─▶ rules_v1  (规则 labeler,基线)          │
        ├─▶ rules_v2  (规则 labeler,当前)          │  三个 label_version
        └─▶ llm_v1    (LLM 只重判 topic_key)       │  并存,可逐行 diff
                                                   │
        ┌──────────────────────────────────────────┘
        ▼
   analytics/
     ├ descriptive   日计数、语言、来源域名
     ├ trend         窗口对比(当前已冻结发布)
     ├ nullmodel     置换检验,两套零模型
     ├ broadcast     行为式广播账号检测
     └ actor         二维 actor typology(受众 × 功能)
        ▼
   agent → reports/*.md(落盘,目前没有真实读者)
```

### 3.2 数据模型要点

- `posts` — 原始帖,含 `raw_payload` JSONB 逐字保留
- `post_labels` — 主键 `(post_id, label_version)`,**标签永不覆写**
- `author_profiles` — 主键 `(author_id, profile_version)`,含 `audience` /
  `function` / 派生的 `actor_type`
- `collection_runs` — 每次采集的 mode / status / 窗口 / 计数 / 错误
- `analysis_runs` — 每次分析的 label_version / query_version / 方法标识 / 时间戳

### 3.3 五条硬约束(贯穿全部代码)

**P1 原始数据永不丢弃。** `raw_payload` 逐字保留,唯一例外是剥离 U+0000
(Postgres 不接受)。

**P2 标签永不覆写。** 新 labeler 作为新 `label_version` 与旧的并存。
`rules_v1` / `rules_v2` / `llm_v1` 三份同时在库里。

**P3 每个数字带 provenance。** 任何分析输出写入 `analysis_runs`,
带版本标识和方法标识。

**P4 时间永远 UTC。**

**P5 系统故障必须与社会趋势可区分。** 所有采集路径写 `collection_runs`。
「某天帖子数掉一半」必须能区分是社会现象还是采集器挂了。

### 3.4 伦理边界

项目声明:**不做去匿名化、不做 profile 聚合、不追踪个人。**
author identifier 仅用于检测发帖集中度,是方法学控制而非研究对象。

因此明确禁止:根据 profile / handle / 关注图推断作者地理位置。
加拿大相关性只能从**帖子内容里的实体**判定,不能从作者身份判定。
允许使用的作者级量仅限:发帖数、活跃天数、公开 follower 数、engagement。

(曾有一个「注册 20 个小号扩大访问」的提议,被拒绝,理由是违反平台条款。)

---

## 4. 已经测出来的结论

### 4.1 趋势:测不出来,而且量化了差多远

用窗口对比(前 15 天 vs 后 15 天),最大变化是 `data_breach` +10 条(+56%)。

但做了置换检验,**两套零模型**:

1. **post-level** —— 在窗口内重排帖子。条件在 `n_cur` / `n_prev` 上,
   所以它检验的是**组成**变化,不是**总量**变化。
2. **author-level** —— 对每个作者的全部发帖做整体循环日移位。
   这保留了作者的发帖量、主题构成和突发结构,只破坏日历对齐。
   **这是 cluster-robust 的版本**,因为同一个作者的帖子不独立。

结果:`data_breach` 在 author-level 零模型下 `p_fwer = 0.718`,
噪声带 ±28 条。用 max-statistic 做族错误率校正,p 值下限 `(b+1)/(m+1)`。

**换了 `llm_v1` 标注之后,+56% 塌成 +22%**(`p_fwer = 0.734`)。
也就是说那个醒目的百分比主要是小分母假象。

**同时否决了 z-score:** 实测的 max|z| 在 0.2–0.9,而纯泊松模拟的
max|z| 中位数是 1.4–1.7、95 分位 3.0–4.0。**真实数据的 z 比纯噪声还小**,
这个统计量在这个规模上是反信息的。

### 4.2 功效分析:需要约 8 倍数据

不是假设 √k 缩放,是**实测**的:把作者池复制 k 份重采样,测出检验功效
随 k 的实际变化曲线,验证了 √k 关系而不是假定它。

结论:达到可检出需要每窗口约 1,264 条 relevant,现在是 158 条。
路径是把窗口拉到 ~56 天(约 4 个月语料),或者把量提高 8 倍。

### 4.3 广播账号:3% 的账号产出 24.3% 的语料

行为式判据(不看 profile,不猜 bot):
`n_posts ≥ 5 ∧ n_active_days ≥ 4 ∧ zero_engagement_rate ≥ 0.6`

其中 zero_rate 只在 `采集滞后 ≥ 24h` 的帖子上算 —— 因为 `like_count`
是采集时刻的快照,刚发出来的帖子必然是 0 赞,不控制这个会把
「采得早」误判成「没人理」。**查过这个混淆不驱动结果**
(广播账号平均滞后 359h,非广播 323h,方向反而相反)。

结果:372 个作者里 11 个(3.0%)产出 24.3% 的语料,engagement 是其余的 1/27。

### 4.4 actor typology:改成二维

原来是一个 `is_broadcast` 布尔。问题是它把「机器情报信号」和「噪声」
划了等号,而这两者在研究上完全不同。

先试了**行为式自动化推断**(发帖间隔熵、时段分布),**否决掉了**:
在小样本上熵有天花板,一个高频发帖的记者和一个 bot 在统计上不可区分。
这是测量问题,不是模型问题,换算法救不了。

改成两个正交轴:

- **audience(测量得到)** — broadcast / engaged / unheard,来自 engagement 分布
- **function(内容判定)** — incident_feed / promotion / commentary / ...,
  由 LLM 读该作者最多 8 条帖子判定

派生 `actor_type`。关键数字:**`organic_attention` 只占语料的 5.4%。**

一条硬规则:`unclassified` 永远不排除。它的含义是「样本量不足以判断」,
不是「判断为无关」——把不确定当阳性会系统性压低低产账号,
而那恰好是最像有机讨论的一批。

用 Mann–Whitney U 验证了这个二维拆分有经验依据:
intel feed vs individual `p = 1.55e-07`(rank-biserial +0.383);
**intel feed vs promotion `p = 1.2e-02`**(+0.185)—— 两类「非有机」账号
彼此也显著不同,支持不该把它们合并成一个 `is_broadcast`。

### 4.5 `other` 的开放编码(120 条人工)

原来 `other` 占 44%。随机抽 120 条(确定性 salt)人工开放编码:

- **MISS 30%** —— 本该落在已有桶里,规则没匹配上。根因:规则匹配术语,
  新闻写自然语言(「法院记录被访问」从不出现 "data breach" 字样)
- **NEW 56%** —— 分类法覆盖不到。其中 78% 集中在两个簇:
  社区活动 / 地缘政治与数据主权
- **FP 12%**,**GENERIC 3%**

### 4.6 `llm_v1`:规则 → LLM 的主题重判

只重判 `topic_key`,`is_cyber` / `is_canada` / `is_relevant` **逐字继承规则的判断**。

评测(在 120 条人工编码上):recall 89%(`data_breach` 23/23),
restraint 96%(该判 other 时判 other),Cohen's κ = 0.870。
对照:STINER 报告 0.84。

**但这个 κ 不是独立验证** —— 人工编码者同时是写 prompt 的人,
且两者用同一套分类法。独立标注仍然是最高优先级的未做事项。

全语料:650 条里 215 条改判,`other` 44.2% → 31.4%,
`vulnerability_cve` 从 8 条变成 0(全部是 "CVE" 字符串的假匹配)。

### 4.7 逐词产出(离线重建)

搜索词基本是文本匹配,所以可以在已有 91,551 条上回溯归因。按 relevant 排序:

| term | 返回 | relevant | 独家 |
|---|---:|---:|---:|
| cybersecurity | 22,498 | **200** | 15,071 |
| ransomware | 8,881 | 105 | 6,172 |
| data breach | 1,939 | 90 | 1,488 |
| hacked | 9,314 | 53 | 8,814 |
| identity theft | 706 | 36 | 671 |
| cyberattack | 2,761 | 21 | 1,511 |
| **CVE** | **12,502** | **3** | 8,707 |
| (11 个词) | — | **0** | — |

零产出的 11 个词:infostealer, botnet, supply chain attack, account takeover,
vulnerability exploit, credential stuffing, rançongiciel, hameçonnage,
logiciel malveillant, password breach, maliciel(这个连一条帖都没返回过)。

两个结论:
- `CVE` 精度 0.024%,单独贡献 13.7% 的采集体积换来 3 条 relevant
- `cybersecurity` 一个词占 relevant 的 30.8%、采集量的 24.6%,**单点依赖**

**这 34 个词的来源:** 项目原始 spec 只写了四对例词加一个「等」,
其余 26 个是实现时凭领域直觉补的。**没有外部来源、没有先导测量、没有验证,
且从第一天起一个字没改过。**

### 4.8 全库加拿大实体筛(今天刚跑完)

动机:`is_relevant = has_cyber_term AND has_canada_term AND corroborated`,
两侧都是窄工具且用 AND 连接,所以召回上限是两个词表的交集。
那 89,714 条被拒的帖子从来没有被任何语义判定看过。

做法:对每条被拒帖问一个问题 —— **有没有加拿大实体?**
(不问 cyber,因为语料里每条都是安全词搜来的,cyber 由采集边界保证。)

先试了联合版本(cyber ∧ Canada),**失败**:4B 模型把合取坍缩成 cyber,
通过率 6.8% 且绝大多数与加拿大无关(Fortinet CVE、法国教育部、
Lynx 受害者名单)。拆成单问后:

```
筛 83,370 条(length > 40)  通过 901 (1.08%)  错误 0  11.1 分钟  125 posts/s
```

**归因表(这批帖当初被哪一环挡下):**

| rules_v2 的判断 | 条数 | 占比 |
|---|---:|---:|
| `is_cyber=T, is_canada=F` —— canada 词表漏了实体 | 503 | **55.8%** |
| 两边都判否 —— 多半筛子误报 | 227 | 25.2% |
| `is_cyber=F, is_canada=T` | 171 | 19.0% |
| **被 corroboration 挡下** | **0** | **0%** |

抓对的样本很能说明形态:魁北克网络安全厅长的姓名、`hhllp.ca` 这类域名、
"GC website"(Government of Canada 的口语缩写)——**全是词表覆盖不到的实体形态**。

筛子自己的误报也很典型:`Canonical distributions`(Linux 发行版)
被当成 Canada —— 和 "GTA = Grand Theft Auto" 完全同构的词形混淆,
只是这次犯错的是 4B 模型不是正则。

**901 条尚未人工审。** 已抽出 200 条分层样本待标注。

### 4.9 否决掉的分析方向(都有测量依据)

- **扩散网络** —— relevant 内只有 73 条回复边,其中 10 条的父帖也 relevant。
  没有可画的网络。
- **共享链接网络** —— 121 个作者里 115 个只出现在一个共享域名里,
  transitivity 0.907。这是一堆互不相连的团,不是网络。**换了图的形式**
  (改成来源采纳图),不是调布局参数。
- **BERTopic / embedding 聚类处理 `other`** —— n=204 上 HDBSCAN 会把大部分
  扔进噪声簇。直接人工开放编码更可解释。
- **firehose / Jetstream** —— 约 24GB/天 × 逐字永久存储与 P1 正面冲突;
  为省盘在 ingest 处过滤等于重造一条不可逆采集边界。

---

## 5. 当前的问题(按我认为的重要性排序)

### P0 — 采集边界是真正的约束,不是判定器

91,551 条采集,650 条 relevant(0.72%)。语料本身 99% 是全球网安噪声,
因为 34 个词全是安全术语、零个加拿大词。

**后果:任何不含那 34 个词的加拿大网安帖,物理上不可达。**
一条「RCMP 说我们系统被入侵」如果措辞没撞上那 34 个词,永远看不到。

§4.8 量出来的召回缺口(901 条待审,估计真实相关的在 150–450 之间)
只是「采进来了但被判定器丢掉」的部分。**「压根没采进来」的部分量不到**,
而那大概率是大头。

外部佐证:Tweezers(NDSS 2025)在 529 万条推文上明说这个边界不可修复 ——
词表没覆盖的事件采到的帖子太少,连簇都形不成。

### P1 — 所有精度/召回数字都挂在自己身上

`κ = 0.870`、`recall 89%`、`restraint 96%` —— **标注者同时是写 prompt 的人,
且用同一套分类法**。这不是 gold set,是第二个相关的 proxy。

而且现有评测集 99 条全部来自 backfill,incremental 完全未评估。

### P2 — 没有任何外部参照物

系统从未和一个「不是自己产生的、带时间戳的、无法影响的」事件登记表对过。
没有 `cyber.gc.ca` advisory、没有 CISA KEV、没有加拿大媒体 RSS。

后果是**「我们看见了多少」这个问题没有分母**。

### P3 — 分类法覆盖不足

`other` 31.4%。开放编码显示 56% 是分类法缺口,其中 78% 集中在两个未建的桶
(社区活动 / 地缘政治与数据主权)。

### P4 — 硬编码与泛化

- 字符串 `Canada` 出现在 11 个 Python/SQL 文件里
- **分类法有两个真值来源**:一份 YAML(给规则 labeler),
  一份 Python 元组(给 LLM 的 guided decoding enum)。两份可以漂移,
  漂移了不会报错,后果是两个 `label_version` 分类到不同的空间上
- 三段 prompt 硬编码在 Python 里,**且 prompt 没进 provenance** ——
  P3 要求每个数字可追溯,但 `llm_v1` 的输出 100% 由 prompt 决定,
  改一个词结果就变,而 `analysis_runs` 里没有 prompt 的任何记录

### P5 — 阈值大多没有依据

| 常量 | 值 | 依据 |
|---|---|---|
| `MIN_SETTLED_LAG_H` | 24 | ✅ 测过采集滞后混淆 |
| `MIN_POSTS_FOR_PATTERN` | 3 | 观察驱动(61 个 incident_feed 里 38 个是单帖转发者) |
| `ENGAGED_MIN_MEAN` | 1.0 | ❌ 无 |
| broadcast `zero_rate ≥ 0.6` | 0.6 | ❌ 无 |
| broadcast `n_posts ≥ 5, n_days ≥ 4` | 5 / 4 | ❌ 无 |

没有任何敏感性分析。不知道结论在这些参数的合理区间内会不会翻转。

### P6 — 没有消费者

`reports/*.md` 落盘,没有人读。

### P7 — 单平台

Bluesky 是一个便利样本。没有跨平台验证,也没有任何关于
「Bluesky 上的加拿大网安讨论能代表什么」的外部证据。

---

## 6. 计划

### 里程碑 0(最先做,因为它决定后面值不值得做)

**建外部事件登记表,回填对齐 8/22–9/22 窗口,量出分子。**

灌入 `cyber.gc.ca` advisory RSS、CISA KEV、加拿大主流媒体 RSS。
回答一个问题:**这 30 天里,加拿大公开了多少起网安事件?**

这个数现在没人知道。如果是 3 起,那么「每日事件简报」这个产品形态
价值接近零,正确动作是把结论写成方法学论文;如果是 15–30 起,日报就立得住。
**它决定后面三批工作的取舍,一周能做完。**

### 然后,按顺序

1. **合并分类法的两个真值来源**(半天,消掉一个静默 bug 类)
2. **抽 `studies/canada_cyber.yaml`,`is_canada` → `in_scope(post, scope)`**
   —— 关键点是让 **scope 同时生成查询流和判定器**。
   现在的结构性 bug 是这两个东西分开硬编码,所以才会出现
   「判定器要求的实体,采集器从来不去找」。从一份 spec 生成之后,
   这个缺口在构造上不可能再出现。
3. **prompt 入 spec + sha256 入 provenance**(半天)
4. **`ccint sensitivity` 子命令** —— 扫每个阈值,输出**结论是否翻转**。
   判据:如果一个结论在参数合理区间内会翻转,那它不是发现,是参数选择。
5. **加拿大侧独立查询流**(新 `query_version`,与现有安全词流并行 OR,
   不是 AND 过滤,所以是严格召回增加)
6. **独立标注 + PPI 区间估计**(不依赖前面任何一步,可并行)

### 终点定义

**一个能报出分母的加拿大网安事件监测器。**
不是「告诉你发生了什么」,是「告诉你发生了什么,并且告诉你我看不见多少」。

完成判据(五条全是「让某个量从不可观测变成有数」,没有一条是「提高准确率」):

1. 能说出「加拿大流独家抓到 X 条相关帖」
2. 能说出「本月我们看见了权威源事件的 M/N」
3. 每个 precision/recall 背后有非本系统作者的标注
4. `21.3/day` 从点估计变成区间
5. 有一个真实收件人每天收到它

---

## 7. 想讨论的问题

按我自己最不确定的排序。

**Q1. 「可测的覆盖率」算不算一个真的贡献?**
所有 CTI 系统和社媒监测研究报的都是分子(找到 N 个事件、recall 0.5563),
没人报分母,因为分母要求承认自己的采样边界。我打算把这个当成主要贡献。
但这可能只是把「我们数据不够」重新包装成「我们诚实」。
**外部判断:这个定位站得住吗?一个审稿人会怎么看?**

**Q2. 650 条/月的体量,事件级输出到底可不可行?**
在做里程碑 0 之前这是未知数。如果真实分子是个位数,
应该转向方法学论文还是继续加采集?**什么样的绝对事件数才撑得起一个产品形态?**

**Q3. 抽 scope spec 是必要重构还是过度工程?**
这是一个单地区的研究,永远不会真的换成「EU」或「Ontario 医疗」。
泛化的唯一实际收益是「让加拿大这一个做对」(查询流与判定器同源)。
**这个收益值不值一到两天,还是应该直接打补丁加一条加拿大查询流?**

**Q4. cluster 级广播过滤的阈值怎么定?**
有人建议用 `unique_authors(C) / posts(C) ≥ 0.80`,这是从一个百万级语料的
系统搬来的。但在 650 条上簇非常小:**任何 ≤5 条的簇里只要一个作者发了两次,
比值就掉到 0.80 以下** —— 而那恰恰最像有机讨论。
我倾向改成「单作者最大占比 ≤ 0.5,且只对簇大小 ≥ 4 生效」。
**小样本下这类比值型阈值有没有更正规的做法?**

**Q5. 多标签主题会打断置换检验的零模型。**
现在每帖一个 topic,而已有工作指出这些安全类别在实践中不互斥
(某研究 10,392 条标注里 9.84% 是多标签)。改成多标签是对的,
但组成式零模型假设计数对 n 封闭,多标签后不成立。
**多标签计数的正确零模型是什么?按 1/k 分摊够吗?**

**Q6. 当 proxy 和 gold 共享同一套分类法时,PPI 还成立吗?**
PPI 的卖点是对模型正确性不作任何假设。但如果人工标注者和 LLM 用的是
同一套由同一个人写的分类法,**共享的分类法错误会不会同时污染两边、
让区间假性收窄?**

**Q7. 单平台是不是致命的?**
有工作量化过:在某些类别上去掉一个平台会损失最多 50% 的报告。
**最便宜的可信跨平台检查是什么?** 我不想为了体面去接三个 API。

**Q8. 趋势层该冻结还是该留着当评估器?**
它已经完成使命(证明当前窗口无可检出趋势),而缺口是 8 倍,
统计方法改进乐观收益只有 1.5–2 倍。但它也是唯一能回答
「事件轨产出的东西是不是真信号」的工具。
**冻结「发布」而保留「内部运行」是对的折中吗?**

**Q9. 伦理约束会不会让加拿大定位从根上受限?**
不做作者级地理推断,加拿大相关性只能从内容实体判定。
这意味着一个加拿大人讨论加拿大的事但没提任何加拿大实体,系统看不见。
**这个限制有多大?有没有在不碰 profile aggregation 的前提下的补救?**

---

## 8. 参考文献(全部核过原文)

- **Tweezers** — Cui et al., *A Framework for Security Event Detection via
  Event Attribution-centric Tweet Embedding*, NDSS 2025。
  529 万推文 / 122 万用户。词表来自合作威胁情报公司的分析师,不自己构造。
  与前人 W2E 词表对比:自己采到的推文中 44.83% 命中 W2E 词表的 83.1% 的词,
  即**一半以上用 W2E 词表抓不到**;W2E 词表缺 `phishing` / `cve` / `patch`。
  151 个新闻事件中检出 84(SONAR 45,W2E 4)。
- **Khandpur et al.**, *Crowdsourcing Cybersecurity: Cyber Attack Detection
  using Social Media*, 2017。按攻击类型手选种子 + Dynamic Query Expansion
  (依存树 + word2vec)。51.4 亿推文 → 7950 万过滤后。
  data breach P=0.78/R=0.74,DDoS P=0.80/R=0.45。
- **Bozarth & Budak**, *Keyword expansion techniques for mining social
  movement data on social media*, EPJ Data Science 2022。
  5 种扩词流水线 × 5 个运动 × 2 种活跃度。词嵌入方法胜过更复杂的新方法;
  高峰期单一流水线只能找到约一半相关推文。
- **WordPPR**, Communication Methods and Measures 2023。
  研究者驱动 + 图上迭代的选词方法。
- **STINER** — cyber social media 上的 actionable entity 抽取。κ = 0.84。
- **AIGT Monitoring**, ACL 2025 —— 成熟的纵向社媒监测研究,
  detector 构建 + 长期追踪 + topic/engagement/user 分布比较。
- **TIBlender** —— agentic CTI;量化了跨平台贡献,
  某些类别去掉一个平台损失最多 50% 的报告。
- **SIA (Social Insight Agents)** —— social-media analysis agent 架构。
- **Sabottke et al.**, USENIX Security 2015;**Huang & Ban**, IEEE TrustCom 2020
  —— 传统 cyber social mining 管线。

---

## 9. 工程现状

- 236 个测试全绿
- Python + PostgreSQL + 本地 vLLM(Qwen3-4B-Instruct on RTX 4080 SUPER)
- 本地筛选吞吐实测 125 posts/s,全库 8.3 万条 11 分钟 —— **算力不是约束**
- 已有 21 页 write-up + 3 页 TL;DR(LaTeX / Overleaf,已上 GitHub 私有仓库)
- 已有 6 张图:主题轨迹、raw vs organic、主题 × 时间热力图、
  actor–topic 网络、来源采纳图、signature chart
