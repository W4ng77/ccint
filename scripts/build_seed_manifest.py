"""冻结 interaction 实验的种子集。见 docs/interaction_experiment/。

只读:不写库、不改任何标签。输出一行一帖(主种子集 ∪ 敏感性种子集),
外加一份 meta.json 记录冻结时刻、行数、sha256 与 git commit,之后的邻域采集
一律以这份文件为准,而不是重新查库 —— 否则判定门一重跑种子集就悄悄变了。

provenance_status 的取值:
  derived_validated        llm_v2c 由 llm_v2b + mechanical_v1 派生(apply_entity_validation.py),
                           prompt/model 取自 llm_v2b 的 label_provenance
  direct_unvalidated_pass  llm_v2c 由 llm_v2_runner 直接写入,**未经** mechanical_v1;
                           离线补跑校验后仍为 relevant
  direct_unvalidated_fail  同上,但补跑校验后**不再** relevant
  deterministic_rules      仅在 rules_v2 种子集中;规则确定性,无 prompt/model
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
from ccint.labelers.llm_v2 import validate_entities

PRIMARY, SENSITIVITY, SOURCE = "llm_v2c", "rules_v2", "bluesky"
OUT = Path("manifests/interaction_experiment/v1")
# vLLM 以 --served-model-name user 起服务(README「运行」一节),库里只记了 "user"
SERVED_MODEL = {"user": "Qwen/Qwen3-4B-Instruct-2507"}

COLUMNS = [
    "post_id", "post_uri", "author_id", "created_at", "source",
    "seed_label_version", "seed_label", "annotation_run_id", "prompt_hash",
    "model_name", "provenance_status", "included_primary", "included_sensitivity",
    # 以下为补充列
    "model_name_resolved", "label_llm_v2c", "label_rules_v2", "passes_mechanical_v1",
]

SQL = """
SELECT p.post_id, p.source_post_id AS post_uri, p.author_id, p.published_at, p.text,
       a.is_relevant AS rel_primary, a.is_cyber, a.matched_terms,
       b.is_relevant AS rel_sens,
       coalesce(pa.label_version, pb.label_version) AS prov_version,
       coalesce(pa.prompt_sha256, pb.prompt_sha256) AS prompt_hash,
       coalesce(pa.model_id, pb.model_id) AS model_id,
       coalesce(pa.recorded_at, pb.recorded_at) AS prov_at,
       pa.post_id IS NOT NULL AS direct
FROM posts p
LEFT JOIN post_labels a ON a.post_id = p.post_id AND a.label_version = %(pv)s
LEFT JOIN post_labels b ON b.post_id = p.post_id AND b.label_version = %(sv)s
LEFT JOIN label_provenance pa ON pa.post_id = p.post_id AND pa.label_version = %(pv)s
LEFT JOIN label_provenance pb ON pb.post_id = p.post_id
     AND pb.label_version = a.matched_terms->>'derived_from'
WHERE p.source_key = %(src)s AND (a.is_relevant OR b.is_relevant)
ORDER BY p.post_id
"""


def lab(v):
    return "unlabeled" if v is None else ("relevant" if v else "irrelevant")


def main() -> None:
    for p in (OUT / "seed_manifest.csv", OUT / "seed_manifest.meta.json"):
        if p.exists():
            sys.exit(f"{p} 已存在,拒绝覆盖(冻结的清单只追加新版本,不重写)")
    OUT.mkdir(parents=True, exist_ok=True)
    frozen_at = datetime.now(timezone.utc)
    with db.connect() as c:
        rows = c.execute(SQL, {"pv": PRIMARY, "sv": SENSITIVITY, "src": SOURCE}).fetchall()

    out = []
    for r in rows:
        prim, sens = bool(r["rel_primary"]), bool(r["rel_sens"])
        passes = ""
        if prim:
            mt = r["matched_terms"] or {}
            if r["direct"]:
                keep, _ = validate_entities(r["text"] or "", mt.get("canada_entities") or [])
                ok = bool(r["is_cyber"]) and bool(keep)
                passes = str(ok).lower()
                status = "direct_unvalidated_pass" if ok else "direct_unvalidated_fail"
            else:
                assert mt.get("validation") == "mechanical_v1", r["post_id"]
                passes, status = "true", "derived_validated"
            run_id = f"{r['prov_version']}@{r['prov_at'].isoformat()}"
            prompt_hash, model = r["prompt_hash"], r["model_id"]
        else:
            status, run_id, prompt_hash, model = "deterministic_rules", "", "", ""
        out.append({
            "post_id": r["post_id"], "post_uri": r["post_uri"], "author_id": r["author_id"],
            "created_at": r["published_at"].isoformat(), "source": SOURCE,
            "seed_label_version": PRIMARY if prim else SENSITIVITY, "seed_label": "relevant",
            "annotation_run_id": run_id, "prompt_hash": prompt_hash, "model_name": model,
            "provenance_status": status,
            "included_primary": str(prim).lower(), "included_sensitivity": str(sens).lower(),
            "model_name_resolved": SERVED_MODEL.get(model, model),
            "label_llm_v2c": lab(r["rel_primary"]), "label_rules_v2": lab(r["rel_sens"]),
            "passes_mechanical_v1": passes,
        })

    path = OUT / "seed_manifest.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out)

    count = lambda k, v: sum(1 for o in out if o[k] == v)
    meta = {
        "frozen_at": frozen_at.isoformat(),
        "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                     text=True).stdout.strip(),
        "source": SOURCE, "primary_label_version": PRIMARY,
        "sensitivity_label_version": SENSITIVITY,
        "created_at_semantics": "posts.published_at (UTC; createdAt corrected by indexedAt per migration 002)",
        "annotation_run_id_semantics": "<provenance label_version>@<label_provenance.recorded_at>; "
                                       "no annotation-run table exists, this is a batch identifier",
        "n_rows": len(out),
        "n_primary": count("included_primary", "true"),
        "n_sensitivity": count("included_sensitivity", "true"),
        "n_both": sum(1 for o in out if o["included_primary"] == o["included_sensitivity"] == "true"),
        "provenance_status": {s: count("provenance_status", s) for s in
                              ("derived_validated", "direct_unvalidated_pass",
                               "direct_unvalidated_fail", "deterministic_rules")},
        "window": [min(o["created_at"] for o in out), max(o["created_at"] for o in out)],
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    (OUT / "seed_manifest.meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
