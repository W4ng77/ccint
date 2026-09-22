#!/usr/bin/env python
"""用 eval/other_sample.tsv 的人工编码评估 llm_v1 的主题分类。

三个指标回答三个不同的问题：
  A 召回  —— 规则漏掉、人工判定属于已有桶的帖子，模型找回来了吗？
  B 克制  —— taxonomy 里确实没有的帖子，模型有没有硬塞进桶？
  C 旁证  —— 模型自报的 off_topic 与人工的 FP 判定吻合吗？

A 和 B 必须一起看。只看 A 会奖励「什么都往桶里塞」的模型。
"""
from __future__ import annotations

import pathlib
import sys
from collections import Counter, defaultdict

from ccint import db

GOLD = pathlib.Path(__file__).resolve().parent.parent / "eval" / "other_sample.tsv"


def load_gold() -> dict[int, str]:
    out = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "post_id")):
            continue
        pid, code = line.split("\t")[:2]
        out[int(pid)] = code
    return out


def main(version: str = "llm_v1") -> int:
    gold = load_gold()
    with db.connect() as c:
        rows = {r["post_id"]: r for r in c.execute(
            """SELECT post_id, COALESCE(topic_key,'other') tk,
                      (matched_terms->>'off_topic')::bool ot
               FROM post_labels WHERE label_version=%s AND post_id = ANY(%s)""",
            (version, list(gold))).fetchall()}
    missing = set(gold) - set(rows)
    if missing:
        print(f"⚠️  {len(missing)} 条尚未由 {version} 判定，先跑 `ccint label-llm`")
        return 1

    miss = {p: g[len("MISS_"):] for p, g in gold.items() if g.startswith("MISS_")}
    hit = sum(1 for p, w in miss.items() if rows[p]["tk"] == w)
    left = sum(1 for p in miss if rows[p]["tk"] == "other")
    per = defaultdict(lambda: [0, 0])
    for p, w in miss.items():
        per[w][1] += 1
        per[w][0] += rows[p]["tk"] == w

    new = [p for p, g in gold.items() if g.startswith("NEW_")]
    kept = sum(1 for p in new if rows[p]["tk"] == "other")
    forced = Counter(rows[p]["tk"] for p in new if rows[p]["tk"] != "other")

    fp = [p for p, g in gold.items() if g == "FP"]
    nonfp = [p for p, g in gold.items() if g != "FP"]
    flagged = sum(1 for p in fp if rows[p]["ot"])
    alarm = sum(1 for p in nonfp if rows[p]["ot"])

    print(f"label_version={version}   评估集 {len(gold)} 条 "
          f"(gold: eval/other_sample.tsv)\n")
    print(f"【A 召回】规则漏检的 {len(miss)} 条")
    print(f"   正确归桶 {hit}/{len(miss)} = {100*hit/len(miss):.0f}%"
          f"   仍留 other {left}   归错桶 {len(miss)-hit-left}")
    print("   " + "  ".join(f"{k} {v[0]}/{v[1]}" for k, v in
                            sorted(per.items(), key=lambda kv: -kv[1][1])))
    print(f"\n【B 克制】taxonomy 确无对应的 {len(new)} 条")
    print(f"   正确留 other {kept}/{len(new)} = {100*kept/len(new):.0f}%"
          f"   硬塞进桶 {len(new)-kept}")
    if forced:
        print(f"   塞去了: {dict(forced)}")
    print(f"\n【C 旁证】off_topic 对比人工 FP")
    print(f"   FP 召回 {flagged}/{len(fp)} = {100*flagged/len(fp):.0f}%"
          f"   非 FP 误报 {alarm}/{len(nonfp)} = {100*alarm/len(nonfp):.1f}%")

    # 单一数字：召回与克制的调和平均，防止只优化一边
    f1 = 2 * (hit/len(miss)) * (kept/len(new)) / ((hit/len(miss)) + (kept/len(new)))
    print(f"\n   召回/克制 调和平均 = {f1:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "llm_v1"))
