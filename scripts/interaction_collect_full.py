"""完整邻域采集(SPEC §13):P_collect 全部 2,631 个种子。试点评审通过后才跑。

    python scripts/interaction_collect_full.py            # 新 run,采全部种子
    python scripts/interaction_collect_full.py --resume   # 只补采此前 full run 里没有结局记录的种子

每个种子单独提交,中断后用 --resume 在新 run 里补齐;分析时按种子取最近一次
非 failed 的结局。只用 CPU / 网络,不跑任何模型。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ccint import db
from ccint.interaction import manifest as mf
from ccint.interaction.collect import git_commit, run
from ccint.interaction.derive import derive_run

OUT = Path("outputs/interaction_experiment/collection")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    m = mf.load("v2")
    seeds = m.population("in_collect")
    with db.connect() as c:
        done = {r["seed_post_id"] for r in c.execute("""
            SELECT DISTINCT o.seed_post_id FROM interaction_seed_outcomes o
            JOIN neighborhood_runs n ON n.run_id = o.collection_run_id
            WHERE n.purpose = 'full' AND n.manifest_sha256 = %s""", (m.sha256,))}
    if done and not a.resume:
        sys.exit(f"已有 full run 覆盖 {len(done)} 个种子;补采请用 --resume")
    todo = [s for s in seeds if int(s["post_id"]) not in done]
    print(f"seeds: {len(seeds)}  already: {len(done)}  to collect: {len(todo)}")
    if not todo:
        return
    rid = run(m, todo, purpose="full")
    with db.transaction() as conn:
        n = derive_run(conn, rid, git_commit())
    OUT.mkdir(parents=True, exist_ok=True)
    runs_path = OUT / "full_runs.json"
    runs = json.loads(runs_path.read_text()) if runs_path.exists() else []
    runs.append(rid)
    runs_path.write_text(json.dumps(runs))
    print(f"run {rid}: {len(todo)} seeds, {n} edges")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    main()
