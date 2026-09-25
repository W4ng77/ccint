# 方法推导：从 post 计数到 interaction-aware 测量

写于 2026-09-25。本文件记录 interaction 实验**是怎么从现有结果推出来的**，
而不是把它回溯包装成项目最初的计划。项目最初的研究问题见 [HANDOFF.md](../../HANDOFF.md)
（「加拿大网安讨论的主题结构随时间怎么变」）。v0 规格里 interaction 的位置是：

- schema 里存了 `in_reply_to`、`reply_count`、`repost_count` 这几个字段，但趋势层**只用两个信号**：
  post count 和 unique authors（HANDOFF「[MUST] v0 只有 count 和 unique authors 两个信号」）；
- HANDOFF 在 `is_low_information` 的说明里已经预见到回复上下文的价值：
  「'wow this is insane' 脱离上下文无信息，但作为某个 incident thread 的回复，
  它反映 public reaction」。不过 v0 没有采集 thread，这一点没有落地。

---

## 0.1 原始测量

ccint 按时间观测与 Canada × cyber 相关的社交媒体帖子。对 topic \(k\)、时间 \(t\)：

\[
X_{k,t} = \#\{\text{observed relevant posts about topic }k\}
\]

v0 到 v1 的全部趋势分析（`analytics/trend.py`、报告里的主题排行、14 天视图）
都建立在一个**没有写出来**的假设上：

\[
\Delta X_{k,t} \approx \Delta A_{k,t}
\]

其中 \(A_{k,t}\) 是底层的网安注意力 / 讨论。也就是说，帖子数的变化被当作讨论热度的变化。

## 0.2 动摇这个假设的实证结果

**(a) 勒索软件外部验证（oracle 采集，`collection_runs.mode='oracle'`）**

外部登记表：ransomware.live 中 2026-08-22 → 2026-09-22 的加拿大受害事件
（`external_events`，由 `scripts/ingest_registry.py` 导入）。拿受害组织名去
Bluesky 定向搜索，结果存于 `reports/oracle_events.json`，漏斗由
`scripts/figures_iteration.py::fig_funnel` 计算。2026-09-25 重算结果如下：

| 关卡 | 事件数 |
|---|---:|
| 登记表中的加拿大勒索事件 | 27 |
| 平台上有 cyber 相关帖子 | 26 |
| 其中被 ccint 常规采集覆盖 | 26 |
| 有**非 feed 账号**发帖 | **2** |

这 26 个事件对应 144 条 cyber 相关命中（去重后是 133 个不同帖子，因为有些帖子同时命中多个事件），
来自 15 个账号，其中 12 个是自动化威胁情报 feed。

口径注意：这里的「feed」是 `fig_funnel` 里的启发式定义，满足以下任一条件即算：
(i) 全语料发帖 ≥ 50 条且平均 (like + repost) < 0.5；(ii) 在一份 9 个 handle 的已知 feed 名单中。
它**不是** `author_profiles` 的 `actor_v1` 分型。两套定义之后要做对照，不能混用。

因此：

\[
\text{event visibility} \neq \text{organic discussion}
\]

对事件的高覆盖率，主要说明的可能是信息**分发（syndication）**做得成功，而不是有人在讨论。

**(b) 同方向的既有证据**（README §「actor 分型」）

- `ransomware` topic 在 14 天窗口内 45 条里有 40 条来自 5 个 feed。剔除这些 feed 后，
  该 topic 直接从趋势排行榜上消失。早期报告里 ransomware 的升降，测的其实是 feed 有没有在运行。
- 语料构成里，新闻转述约占 27%，情报 feed 约占 18%。
- 项目已经做了 `ccint trend --actor-mode attention | events` 的二分：前者剔除 feed 和宣传号，
  后者保留 feed。**这就是下面分解模型的前身**，只是之前按账号整体剔除，没有测量交互。

## 0.3 修订后的测量模型

把观测到的量看作几部分的混合：

\[
X_t = D_t + H_t + \epsilon_t
\]

- \(X_t\)：观测到的 cyber 相关活动总量
- \(D_t\)：分发 / 广播 / 转载类活动
- \(H_t\)：人类互动 / 讨论类活动
- \(\epsilon_t\)：不相关、含义模糊的内容，以及测量噪声

约束（均为硬性要求）：

1. **非 feed ≠ organic。** 未被识别为 feed 的帖子仍可能来自机构、媒体或宣传号。
2. **机构账号 ≠ 自动化。** `actor_v1` 已经把 function 轴（incident_feed / news_media /
   promotion / individual / org_other）和 audience 轴分开存储（migration 003），
   原因正是 ecrime.ch 和 bsidesedmonton 这类账号不能归为一类。
3. **首轮标注不强制二分。** 允许 unknown / ambiguous，理由与 migration 006
   把「没问过」和「问过且答否」分开相同。

实验要回答的是：**把 \(D\) 和 \(H\) 分开以后，传感器给出的结论会不会变。**

## 0.4 为什么引入 interaction

现有测量把每条帖子都当作一个独立的观测。新的假设是，社交结构本身也携带信息：

\[
\text{Cyber signal} = f(\text{content}, \text{actor}, \text{interaction}, \text{time})
\]

interaction 可能帮助区分以下几种情形。它们在 post 计数里无法区分，
但在交互结构上有可预期的差别：

| 情形 | 预期的交互结构 |
|---|---|
| 单向广播 | 无回复，或只有零星点赞 |
| 重复转载 | 多个账号近似同文，彼此之间没有回复边 |
| 媒体放大 | 有转发 / 引用，但回复少 |
| 实质讨论 | 回复链 ≥ 2 层，参与者 ≥ 2 人 |
| 问答往来 | 回复中有提问，原作者或第三方作答 |
| 人际扩散 | 引用帖由不同作者发出，并附有自己的评论 |

思路借鉴了 interaction-aware 的社交话语建模，但**这不是复现 ControBench**：
目标是**测量效度**，不是立场分类。

## 0.5 本实验要检验什么（与研究问题的对应）

- RQ1：\(X\) 中 \(D\) / \(H\) / 其他各占多少（描述性）
- RQ2：内容 \(C\)、账号 \(A\)、交互 \(I\) 对「活动类型」各自提供多少信息（消融实验）
- **RQ3（核心）：** 用 \(S^{post}\) 与 \(S^{actor}\)、\(S^{interaction}\)、\(S^{human}\)
  度量时，时间 / 主题 / 事件层面的结论是否实质不同
- RQ4：哪种度量与独立外部参照（ransomware.live、CISA KEV）对得上。
  ccint 自己产出的标签**不能**当作外部 ground truth。

数据上能不能做，见 [DATA_CAPABILITY_AUDIT.md](DATA_CAPABILITY_AUDIT.md)。
