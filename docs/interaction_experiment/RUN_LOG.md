# Run log — interaction experiment

只追加，不改写。时间一律为 UTC。

## 2026-09-25

- **19:29** 生成 v1 种子清单（`scripts/build_seed_manifest.py`）。
  共 2,631 行，sha256 `358fe62447c439e7ef30f49e44543756436d6a807cdfc9ccdde337cb294d83d1`
- **19:37** 由 `llm_v2c` 派生出 `llm_v2d`（`scripts/apply_entity_validation.py --src llm_v2c --dst llm_v2d`）。
  bluesky relevant 为 2,453 条，被剔除 40 条，新增 0 条。详见 `ANNOTATION_VERSION_AUDIT.md`
- **19:41** 生成 v2 种子清单（`scripts/build_seed_manifest_v2.py`）。
  共 2,631 行，sha256 `d2fe0a9e494bf6814d6d03788515dccd030f7cadd9d047acea1e9dc946e7a6ca`
- 两份清单从 `outputs/interaction_experiment/` 移到 `manifests/interaction_experiment/{v1,v2}/`，
  移动前后四个文件的 sha256 完全一致：

  | 文件 | sha256 |
  |---|---|
  | v1/seed_manifest.csv | `358fe62447c439e7ef30f49e44543756436d6a807cdfc9ccdde337cb294d83d1` |
  | v1/seed_manifest.meta.json | `514afa67b0442abfbdcb77a2ac67ae6b48711e00a841383508a7bde369c07f17` |
  | v2/seed_manifest.csv | `d2fe0a9e494bf6814d6d03788515dccd030f7cadd9d047acea1e9dc946e7a6ca` |
  | v2/seed_manifest.meta.json | `6a13a03d0b5d6cf48ad7eb74b2f13b01d041f95aa4a7f4471d18de15fd975502` |

- **v2 被指定为当前生效的冻结清单。** 用 `ccint.interaction.manifest.load("v2")` 直接从文件核对：
  main 2,453 / legacy 2,493 / rules 650 / collect 2,631，与规格一致；sha256 与 meta 一致；没有重复 URI。
- **19:4x** 提交 `9b6f8cc`：包括两版清单、`llm_v2d` 的派生代码、各份审计文档和规格。工作分支为 `interaction-experiment`，没有推送。
- **19:50** 应用 migration `009_interaction_layer.sql`，新建 `neighborhood_runs`、`interaction_snapshots`、
  `interaction_seed_outcomes`、`interaction_edges` 四张表，`collection_runs.mode` 增加 `neighborhood`。
  随后提交 `1b28262`，全部 275 个测试通过。
- **19:52–19:54** 跑邻域试点（`scripts/interaction_pilot.py collect`），collector 为 `neighborhood_v1`：
  - run 470 `pilot`：100 个种子，302 次请求，57.6 秒，结局为成功 96 / partial 3 / unavailable 1 / 失败 0；
  - run 471 `pilot_repeat`：10 个种子，31 次请求；
  - run 472 `pilot_pagination`：5 个种子，page_limit=2，191 次请求。

  详见 `NEIGHBORHOOD_DRY_RUN.md`。
- collector 升级为 `neighborhood_v1.1`：`duplicate_items` 改为跨页累计。
- **停在试点评审前。** 完整的 2,631 个种子采集**尚未开始**。
- **20:22–20:45** 完整邻域采集：run 475（`purpose = full`），collector 为 `neighborhood_v1.1`，commit 为 `20af490`。
  共 2,631 个种子，7,896 次请求，用时 23.9 分钟。结局为成功 2,593 / partial 36 / 不可得 2 / 失败 0，重试 2 次，429 限流 0 次。
  随后派生出 5,988 条边（`edges_v1`）。
- 运行描述性分析（`scripts/interaction_descriptive.py`），结果见 `RESULTS.md`。
  修正了一处口径问题：初版在 `topic_v2` 缺值时会用 `rules_v2` 的 topic 回填，属于混用两套版本；
  改为只用 `topic_v2`，缺值记为 `unassigned`，然后重跑。
- 抽取人工标注样本（`scripts/build_annotation_sample.py`，随机种子 20260926）：
  共 500 个单元，其中信度子集 150 个；`annotation_items.csv` 的 sha256 为
  `8b76d29c32f305383b71b3fdc25dfad3f7347d749d4b2abec10ca5931d4a49c8`。
- **停在人工标注之前。**
