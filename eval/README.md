# 评测集

## 为什么这里发的是 ID 而不是正文

`canada_screen` 与 `canada_screen_audit` 这两份集合的完整版本包含作者 handle
和帖子正文。它们**不进这个仓库**,仓里发布的是 `*.ids.tsv` —— 只有帖子的平台
URI 和我们的标注,没有正文、没有 handle。

理由不是法律谨慎,是这个项目自己的伦理约束:author identifier 仅用于检测发帖
集中度,是方法学控制而非研究对象;不做 profile 聚合、不追踪个人。把「handle +
正文 + 我们对这条帖的研究判断」配成一张可检索的公开表,正是那条约束要排除的
形态,而公开仓一旦被索引和 fork 就不可撤回。

按 ID 分发、由使用者自行向平台取回原文(rehydration),是社媒数据集的通行做法:
它保留可复现性,而不重新发布内容,也让删帖的作者的删除动作依然有效 —— 这一点
在直接分发正文的数据集里是做不到的。

## 文件

| 文件 | 内容 | 含正文 |
|---|---|---|
| `sample.tsv` | 99 条人工标注,relevance 与 topic | 否 |
| `other_sample.tsv` | 120 条 `other` 桶的开放式编码 | 否 |
| `migration_sample.tsv` | rules_v2 → llm_v1 的分歧样本 | 否 |
| `canada_screen.ids.tsv` | 全库筛出的 901 条候选 | 否(ID 版) |
| `canada_screen_audit.ids.tsv` | 上者的 200 条分层抽样,待独立标注 | 否(ID 版) |

`canada_screen_audit` 的 `human_is_canada` / `human_is_cyber` 两列**故意留空**。
写 prompt 的人不应该是给它打分的人:那样得到的只是两个相关 proxy 的互相印证,
不是验证。这两列等一位未参与本系统的标注者填写。
