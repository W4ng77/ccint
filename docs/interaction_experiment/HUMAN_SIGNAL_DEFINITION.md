# 人类讨论信号 \(H_i\) 的定义（预注册草案）

对应 SPEC §25。**这份映射写于任何人工标注完成之前**（2026-09-25），用途是事先锁定口径，
避免看过时间序列或分类结果之后再回头调整映射。
标注员之间的一致性评估通过（SPEC §26）之后，本映射才会被使用。
如需修改，只能新增版本并写明理由，不能改动本版本。

## 输入

每个标注单元有两个维度：`role`（维度 A）和 `function`（维度 B），定义见 `ANNOTATION_GUIDELINES.md`。
凡是做过裁决的单元，一律使用裁决后的标签。

## 主定义 \(H_i\)（交互功能口径）

按顺序判定，命中即停：

1. `role = automated_feed` → \(H_i = 0\)
2. `function = broadcast_or_repost` → \(H_i = 0\)
3. `function` 属于 {`substantive_reply`, `information_seeking`, `information_providing`, `discussion_or_reaction`} → \(H_i = 1\)
4. 其他情况（`function = unclear`）→ \(H_i = \text{NA}\)

**理由：** 要测的构念是「对话性的参与」和「单向分发」的对立，而 SPEC §22 要求角色和功能分开。
所以机构号或媒体号参与了一段实质性交流，在主定义下也计为 1。
自动 feed 无论内容写成什么形式，都视为分发。

## 敏感性定义 \(H_i^{person}\)（个人发言者口径）

在主定义基础上加一条限制：\(H_i = 1\) 且 `role` 属于 {`security_professional`, `individual_participant`} 时才记为 1。
`role` 属于 {`organizational_broadcast`, `news_media`} 且主定义为 1 的单元，改记为 NA。
其余情况与主定义相同。

## 报告要求

- NA 的比例单独报告，**不**并入 0 或 1。
- \(S^{human}\) 与 \(S^{broadcast}\) 只统计有可信事件时间的单元，也就是回复和引用帖的 `createdAt`；种子帖用 `published_at`。
- 两种定义都要报告；如果结论在两者之间不一致，必须明确写出来。

## 状态

**草案，待用户确认后冻结。** 确认之前，不应产出任何依赖 \(H_i\) 的结果。
