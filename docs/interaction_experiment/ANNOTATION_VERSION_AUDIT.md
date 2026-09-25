# 标注版本审计：`llm_v2c` 的两种口径与 `llm_v2d`

审计日期 2026-09-25。本文件是 interaction 实验种子集定义的依据，
对应的冻结产物为 `manifests/interaction_experiment/v1/` 和 `v2/`。

## 1. 发现

`llm_v2c` 共 93,244 行，是当前门户使用的判定门（`portal/queries.py:13`）。
这一个版本号底下，混着**两种判定流程**产生的标签：

| 来源 | 行数 | 其中 bluesky relevant | 是否经过 `mechanical_v1` 校验 | 溯源记录在哪 |
|---|---:|---:|---|---|
| `scripts/apply_entity_validation.py` 从 `llm_v2b` 派生 | 89,044 | 2,348 | 是 | `llm_v2b` 的 `label_provenance` 行 |
| `llm_v2_runner` 直接写入（2026-09-23 02:41，新增的 incremental 帖） | 4,200 | 145 | **否** | `llm_v2c` 的 `label_provenance` 行 |

`validate_entities`（`labelers/llm_v2.py`）的 docstring 把这一步标为 [MUST]，
因为它能拦下模型编造的实体，以及非 `.ca` 域名这类错误。但 `llm_v2_runner` 里没有调用它。

对那 145 条直接写入的种子帖离线补跑校验，结果有 **40 条**不再判为 relevant（28%）。
原因分布：23 条是实体在原文里找不到，10 条是域名不是加拿大顶级域，7 条两个原因都有。

## 2. 处理

`llm_v2c` 不做任何改动（规格 §48 第 10 条：标注版本永不覆盖）。
新派生的版本 `llm_v2d`，由下面这条命令生成：

    python scripts/apply_entity_validation.py --src llm_v2c --dst llm_v2d

它对 `llm_v2c` 的全部行统一施加 `mechanical_v1` 校验。为此，脚本先做了一处修改：
源标签本身已经是派生版本时，要保留完整的派生链（`matched_terms.derived_chain`）
和之前已丢弃的实体（`dropped_entities` 累加）。不做这个修改的话，89,044 行派生标签
会丢掉指向 `llm_v2b` 的溯源，也就无法再查到 prompt 和模型。

派生结果（查库核对）：

| | `llm_v2c` | `llm_v2d` |
|---|---:|---:|
| 总行数 | 93,244 | 93,244 |
| relevant（全部 source） | 2,502 | 2,462 |
| relevant（bluesky） | 2,493 | **2,453** |
| 从 relevant 变为不相关 | | 40（正好是 §1 的那 40 条） |
| 从不相关变为 relevant | | 0 |
| `derived_chain = [llm_v2b, llm_v2c]` | | 89,044 行，其中 relevant 2,348 |
| `derived_chain = [llm_v2c]` | | 4,200 行，其中 relevant 114 |

## 3. 对种子集的影响

| 人群 | 定义 | bluesky 数量 | 角色 |
|---|---|---:|---|
| \(P_{main}\) | `llm_v2d` relevant | 2,453 | 主推断人群 |
| \(P_{legacy}\) | `llm_v2c` relevant | 2,493 | 敏感性分析：量化 pipeline 不一致的影响 |
| \(P_{rules}\) | `rules_v2` relevant | 650 | 敏感性分析：规则门 |
| \(P_{collect}\) | \(P_{legacy} \cup P_{rules}\) | 2,631 | 邻域采集对象 |

- v1 清单（`seed_manifest.csv`，sha256 `358fe624…`）只包含 `llm_v2c` 和 `rules_v2` 两个人群，作为历史记录保留。
- v2 清单（sha256 `d2fe0a9e…`）是当前生效的清单。

## 4. 仍然存在的问题（未处理）

1. **`llm_v2_runner` 仍然不调用 `validate_entities`。** 如果今后用它为新帖写入标签，同样的不一致会再次出现。
   修复这个问题需要一个新的 label version，不在本实验的范围内，也没有改动。
2. **`model_id` 只记录了 `"user"`**，这是 vLLM 的 `--served-model-name`。按 README 的启动命令，
   它对应 `Qwen/Qwen3-4B-Instruct-2507`，但数据库本身无法证明当时实际加载的是哪个权重。
3. **`topic_v2` 的 2,502 行在 `label_provenance` 中没有记录。** 它覆盖的是 `llm_v2c` 的
   relevant 集合，而 \(P_{main}\) 是它的子集，所以对 \(P_{main}\) 来说 topic 标签都在，只是来源无法追溯。
4. **门户仍然使用 `llm_v2c`**（规格 §49：在主分析完成验证之前不切换）。
