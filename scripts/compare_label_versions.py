#!/usr/bin/env python
"""并排比较两个 label_version 的主题分布与逐条迁移。

[MUST] 两个 version 必须覆盖同一批 post，否则差异里会混进「谁标了谁没标」。
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

from ccint import db


def main(a: str = "rules_v2", b: str = "llm_v1") -> int:
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT la.post_id,
                      COALESCE(la.topic_key,'other') a,
                      COALESCE(lb.topic_key,'other') b,
                      (lb.matched_terms->>'off_topic')::bool ot
               FROM post_labels la
               JOIN post_labels lb ON lb.post_id = la.post_id
               WHERE la.label_version=%s AND lb.label_version=%s
                 AND la.is_relevant""", (a, b)).fetchall()]
    if not rows:
        print(f"没有同时被 {a} 与 {b} 标注的 relevant 帖子")
        return 1
    n = len(rows)
    ca, cb = Counter(r["a"] for r in rows), Counter(r["b"] for r in rows)
    keys = sorted(set(ca) | set(cb), key=lambda k: -cb[k])

    print(f"同时被 {a} 与 {b} 标注的 relevant 帖子: {n}\n")
    print(f"{'topic':26s} {a:>10s} {b:>10s} {'Δ':>7s} {'变化':>8s}")
    print("-" * 66)
    for k in keys:
        d = cb[k] - ca[k]
        pct = f"{100*d/ca[k]:+.0f}%" if ca[k] else "n/a"
        print(f"{k:26s} {ca[k]:10d} {cb[k]:10d} {d:+7d} {pct:>8s}")

    changed = [r for r in rows if r["a"] != r["b"]]
    print(f"\n判定不同: {len(changed)}/{n} = {100*len(changed)/n:.0f}%")

    flow = Counter((r["a"], r["b"]) for r in changed)
    print(f"\n最大的 12 条迁移路径 ({a} → {b}):")
    for (x, y), k in flow.most_common(12):
        print(f"   {x:26s} → {y:26s} {k:4d}")

    out_of_other = sum(v for (x, y), v in flow.items() if x == "other")
    into_other = sum(v for (x, y), v in flow.items() if y == "other")
    print(f"\n   从 other 召回      {out_of_other:4d}")
    print(f"   反向落回 other     {into_other:4d}")
    print(f"   other 占比  {100*ca['other']/n:.1f}%  →  {100*cb['other']/n:.1f}%")

    ot = sum(1 for r in rows if r["ot"])
    print(f"\n{b} 自报 off_topic: {ot} 条 ({100*ot/n:.1f}%)"
          "   —— 仅作旁证，未改写 is_relevant")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:3] or ["rules_v2", "llm_v1"])))
