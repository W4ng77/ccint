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
