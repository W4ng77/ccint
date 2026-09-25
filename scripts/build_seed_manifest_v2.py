"""种子清单 v2:三个分析人群 + 一个采集人群。见 docs/interaction_experiment/。

v1(build_seed_manifest.py 产出的 manifests/interaction_experiment/v1/)是历史 provenance,
不重写。v2 一行一帖,覆盖采集人群 P_collect = P_legacy ∪ P_rules,
各分析人群用 in_* 列标记 —— 采集一次,离线按多种定义分析。

  in_main    llm_v2d relevant   主推断人群
  in_legacy  llm_v2c relevant   量化 pipeline 不一致的影响
  in_rules   rules_v2 relevant  规则门敏感性
  in_collect 恒为 true          邻域采集对象

[MUST] 只读;目标文件已存在则拒绝写入。
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ccint import db

MAIN, LEGACY, RULES, SOURCE = "llm_v2d", "llm_v2c", "rules_v2", "bluesky"
OUT = Path("manifests/interaction_experiment/v2")
CSV_PATH, META_PATH = OUT / "seed_manifest.csv", OUT / "seed_manifest.meta.json"
# vLLM 以 --served-model-name user 起服务(README「运行」一节),库里只记了 "user"
SERVED_MODEL = {"user": "Qwen/Qwen3-4B-Instruct-2507"}

COLUMNS = [
    "post_id", "post_uri", "author_id", "created_at", "source",
    "in_main", "in_legacy", "in_rules", "in_collect",
    "label_llm_v2d", "label_llm_v2c", "label_rules_v2",
    "derived_chain", "annotation_run_id", "prompt_hash", "model_name", "model_name_resolved",
    "provenance_status", "exclusion_reason",
]

SQL = """
SELECT p.post_id, p.source_post_id AS post_uri, p.author_id, p.published_at,
       d.is_relevant AS rel_main, c.is_relevant AS rel_legacy, r.is_relevant AS rel_rules,
       d.matched_terms->'derived_chain' AS chain,
       d.matched_terms->'dropped_entities' AS dropped,
       lp.label_version AS prov_version, lp.prompt_sha256, lp.model_id, lp.recorded_at
FROM posts p
JOIN post_labels c ON c.post_id = p.post_id AND c.label_version = %(legacy)s
LEFT JOIN post_labels d ON d.post_id = p.post_id AND d.label_version = %(main)s
LEFT JOIN post_labels r ON r.post_id = p.post_id AND r.label_version = %(rules)s
LEFT JOIN label_provenance lp ON lp.post_id = p.post_id
     AND lp.label_version = d.matched_terms->'derived_chain'->>0
WHERE p.source_key = %(src)s AND (c.is_relevant OR r.is_relevant)
UNION ALL
-- rules_v2 相关但不在 llm_v2c 标注范围内的帖(若有)
SELECT p.post_id, p.source_post_id, p.author_id, p.published_at,
       NULL, NULL, TRUE, NULL, NULL, NULL, NULL, NULL, NULL
FROM posts p
JOIN post_labels r ON r.post_id = p.post_id AND r.label_version = %(rules)s AND r.is_relevant
WHERE p.source_key = %(src)s
  AND NOT EXISTS (SELECT 1 FROM post_labels c
                  WHERE c.post_id = p.post_id AND c.label_version = %(legacy)s)
ORDER BY 1
"""


def lab(v):
    return "unlabeled" if v is None else ("relevant" if v else "irrelevant")


def flag(v):
    return str(bool(v)).lower()


def main() -> None:
    for p in (CSV_PATH, META_PATH):
        if p.exists():
            sys.exit(f"{p} 已存在,拒绝覆盖(冻结的清单只追加新版本,不重写)")
    OUT.mkdir(parents=True, exist_ok=True)
    frozen_at = datetime.now(timezone.utc)
    with db.connect() as c:
        rows = c.execute(SQL, {"main": MAIN, "legacy": LEGACY, "rules": RULES,
                               "src": SOURCE}).fetchall()

    out = []
    for r in rows:
        chain = (r["chain"] or []) + [MAIN] if r["chain"] is not None else []
        llm_seed = bool(r["rel_legacy"]) or bool(r["rel_main"])
        if llm_seed:
            status = ("derived_validated" if chain[:1] == ["llm_v2b"]
                      else "direct_then_validated")
            run_id = f"{r['prov_version']}@{r['recorded_at'].isoformat()}"
            prompt_hash, model = r["prompt_sha256"], r["model_id"]
        else:
            status, run_id, prompt_hash, model = "deterministic_rules", "", "", ""
        excl = ""
        if r["rel_legacy"] and not r["rel_main"]:
            reasons = sorted({e.get("reason", "?") for e in (r["dropped"] or [])})
            excl = "failed_mechanical_v1:" + "+".join(reasons)
        out.append({
            "post_id": r["post_id"], "post_uri": r["post_uri"], "author_id": r["author_id"],
            "created_at": r["published_at"].isoformat(), "source": SOURCE,
            "in_main": flag(r["rel_main"]), "in_legacy": flag(r["rel_legacy"]),
            "in_rules": flag(r["rel_rules"]), "in_collect": "true",
            "label_llm_v2d": lab(r["rel_main"]), "label_llm_v2c": lab(r["rel_legacy"]),
            "label_rules_v2": lab(r["rel_rules"]),
            "derived_chain": ">".join(chain), "annotation_run_id": run_id,
            "prompt_hash": prompt_hash, "model_name": model,
            "model_name_resolved": SERVED_MODEL.get(model, model),
            "provenance_status": status, "exclusion_reason": excl,
        })

    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out)

    n = lambda k: sum(1 for o in out if o[k] == "true")
    meta = {
        "manifest_version": "v2",
        "supersedes_for_analysis": "seed_manifest.csv (v1, retained unchanged)",
        "frozen_at": frozen_at.isoformat(),
        "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                     text=True).stdout.strip(),
        "source": SOURCE,
        "populations": {
            "P_main": {"label_version": MAIN, "n": n("in_main"), "role": "primary inference"},
            "P_legacy": {"label_version": LEGACY, "n": n("in_legacy"),
                         "role": "sensitivity: pipeline inconsistency"},
            "P_rules": {"label_version": RULES, "n": n("in_rules"),
                        "role": "sensitivity: rule-based gate"},
            "P_collect": {"definition": "P_legacy ∪ P_rules", "n": n("in_collect")},
        },
        "overlaps": {
            "main∩rules": sum(1 for o in out if o["in_main"] == o["in_rules"] == "true"),
            "legacy∩rules": sum(1 for o in out if o["in_legacy"] == o["in_rules"] == "true"),
            "legacy\\main": sum(1 for o in out if o["in_legacy"] == "true" and o["in_main"] == "false"),
            "main\\legacy": sum(1 for o in out if o["in_main"] == "true" and o["in_legacy"] == "false"),
        },
        "provenance_status": {s: sum(1 for o in out if o["provenance_status"] == s)
                              for s in ("derived_validated", "direct_then_validated",
                                        "deterministic_rules")},
        "created_at_semantics": "posts.published_at (UTC; createdAt corrected by indexedAt per migration 002)",
        "annotation_run_id_semantics": "<chain-head label_version>@<label_provenance.recorded_at>; "
                                       "no annotation-run table exists, this is a batch identifier",
        "window": [min(o["created_at"] for o in out), max(o["created_at"] for o in out)],
        "sha256": hashlib.sha256(CSV_PATH.read_bytes()).hexdigest(),
    }
    META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
