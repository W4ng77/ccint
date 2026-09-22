#!/usr/bin/env python
"""从 related work 接进来的两个统计检验。

1. Mann–Whitney U（AIGT ACL 2025 用它比较 AIGT 与人写文本的互动量）
   —— 我们此前只报了均值 0.20 vs 5.34，没有做检验。互动量是重尾且零膨胀的，
   不满足 t 检验的假设，秩和检验是正确的选择。

2. Cohen's κ（STINER 用它报告两位标注者的一致性，得 0.84）
   —— 我们此前只报了「召回 89% / 克制 96%」，那是针对特定子集的比例，
   不是一个可与文献对比的一致性统计量。

[MUST] κ 在这里衡量的是「作者的人工编码」与「llm_v1」的一致性，
**不是独立验证** —— 两者的判据来自同一套 taxonomy，且人工编码者
（本项目作者）也是写 prompt 的人。真正的独立标注仍是最高优先级的未做事项。
"""
from __future__ import annotations

import pathlib
import sys
from collections import Counter

from scipy.stats import mannwhitneyu

from ccint import db

GOLD = pathlib.Path(__file__).resolve().parent.parent / "eval" / "other_sample.tsv"


def cohen_kappa(a: list[str], b: list[str]) -> tuple[float, int]:
    """两个标注序列的 Cohen's κ。"""
    n = len(a)
    labels = sorted(set(a) | set(b))
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[l] / n) * (cb[l] / n) for l in labels)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0, n


def engagement_test(conn) -> None:
    """情报 feed vs 有机讨论的互动量：秩和检验，非均值对比。"""
    rows = conn.execute("""
      SELECT ap.actor_type, (p.like_count + p.repost_count) AS eng
      FROM posts p
      JOIN post_labels l ON l.post_id = p.post_id
      JOIN author_profiles ap ON ap.author_id = p.author_id
      WHERE l.label_version='rules_v2' AND l.is_relevant
        AND ap.profile_version='actor_v1'
        -- [MUST] 只比已沉淀的帖子。like_count 是采集瞬间的快照，
        -- 不控制时滞就是在比较「被采集的早晚」而不是「受众」。
        AND extract(epoch FROM (p.collected_at - p.published_at))/3600 >= 24
    """).fetchall()
    groups: dict[str, list[int]] = {}
    for r in rows:
        groups.setdefault(r["actor_type"], []).append(int(r["eng"]))

    intel = groups.get("intel_feed_raw", [])
    organic = (groups.get("organic_attention", [])
               + groups.get("individual_unheard", []))
    promo = groups.get("promotion_unheard", []) + groups.get("promotion_reaching", [])

    print("=== Mann–Whitney U：互动量分布（已控制采集时滞 ≥24h）===")
    for name, x, y in (("情报 feed vs 个人讨论", intel, organic),
                       ("机构宣传 vs 个人讨论", promo, organic),
                       ("情报 feed vs 机构宣传", intel, promo)):
        if len(x) < 5 or len(y) < 5:
            print(f"  {name}: 样本不足 ({len(x)} vs {len(y)})，跳过")
            continue
        u, p = mannwhitneyu(x, y, alternative="two-sided")
        # rank-biserial 相关：效应量，不受样本量膨胀影响
        rb = 1 - (2 * u) / (len(x) * len(y))
        med = lambda v: sorted(v)[len(v) // 2]                      # noqa: E731
        print(f"  {name}")
        print(f"    n={len(x)} vs {len(y)}   中位数 {med(x)} vs {med(y)}   "
              f"均值 {sum(x)/len(x):.2f} vs {sum(y)/len(y):.2f}")
        print(f"    U={u:.0f}  p={p:.2e}  rank-biserial r={rb:+.3f}")


def kappa_test(conn) -> None:
    gold_raw = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "post_id")):
            continue
        pid, code = line.split("\t")[:2]
        gold_raw[int(pid)] = code

    # 把人工编码折算成主题标签：MISS_x → x；NEW_* → other。
    # FP / GENERIC 排除 —— 它们编码的是相关性与可归纳性，不是主题，
    # 放进 κ 会把两种不同的判断混在一个混淆矩阵里。
    gold = {p: (c[len("MISS_"):] if c.startswith("MISS_") else "other")
            for p, c in gold_raw.items() if c.startswith(("MISS_", "NEW_"))}

    rows = {r["post_id"]: r["tk"] for r in conn.execute(
        """SELECT post_id, COALESCE(topic_key,'other') tk
           FROM post_labels WHERE label_version='llm_v1' AND post_id = ANY(%s)""",
        (list(gold),)).fetchall()}
    pairs = [(gold[p], rows[p]) for p in sorted(gold) if p in rows]
    a, b = [x for x, _ in pairs], [y for _, y in pairs]
    k, n = cohen_kappa(a, b)
    agree = sum(1 for x, y in pairs if x == y) / n

    print("\n=== Cohen's κ：人工编码 vs llm_v1（主题标签）===")
    print(f"  n={n}   一致率={agree:.1%}   κ={k:.3f}")
    print(f"  参照：STINER 两位安全研究员标注 2,100 条推文，span 级精确匹配 κ=0.84")
    print("  ⚠️ 这不是独立验证：人工编码者同时是写 prompt 的人，"
          "且两者用同一套 taxonomy。独立标注仍是最高优先级的未做事项。")


def main() -> int:
    with db.connect(autocommit=True) as conn:
        engagement_test(conn)
        kappa_test(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
