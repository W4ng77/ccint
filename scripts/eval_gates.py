"""判定门在外部评测集上的表现(T0 baseline + T3 对比)。

[MUST] 所有 recall / precision 必须**分层报告**。不分层的总体数字被 feed 层
支配 —— 勒索软件登记表的受害组织名在社交平台上主要出现在转发该登记表的自动
账号里,所以「总体 recall」实际衡量的是「抓到 bot 的能力」,而这个项目的研究
对象是有机讨论。分层之后,非 feed 层的样本量本身就是结论。
"""
from __future__ import annotations

from collections import defaultdict

from ccint import db

VERSIONS = ["rules_v1", "rules_v2", "llm_v2"]


def gate_table() -> dict[str, dict[int, bool]]:
    out: dict[str, dict[int, bool]] = defaultdict(dict)
    with db.connect(autocommit=True) as c:
        for r in c.execute(
                "select label_version, post_id, is_relevant from post_labels "
                "where label_version = any(%s)", (VERSIONS,)):
            out[r["label_version"]][r["post_id"]] = r["is_relevant"]
    return out


def evalset(sid: str) -> list[dict]:
    with db.connect(autocommit=True) as c:
        return [dict(r) for r in c.execute(
            "select post_id, label, stratum from eval_sets where eval_set_id=%s",
            (sid,))]


def score(rows: list[dict], gate: dict[int, bool], positive: bool) -> dict:
    by = defaultdict(lambda: [0, 0, 0])      # stratum -> [n, judged, missing]
    for r in rows:
        s = by[r["stratum"]]
        s[0] += 1
        v = gate.get(r["post_id"])
        if v is None:
            s[2] += 1
        elif v:
            s[1] += 1
    tot = [sum(x[i] for x in by.values()) for i in range(3)]
    out = {"_all": _row(tot, positive)}
    for k, v in sorted(by.items()):
        out[k] = _row(v, positive)
    return out


def _row(v, positive):
    n, judged, missing = v
    scored = n - missing
    if scored == 0:
        return {"n": n, "rate": None, "missing": missing}
    rate = judged / scored if positive else 1 - judged / scored
    return {"n": n, "hit": judged, "scored": scored, "rate": rate,
            "missing": missing}


def main():
    gates = gate_table()
    pos, neg = evalset("E_reg_pos_v1"), evalset("E_reg_neg_v1")
    print(f"E_reg_pos_v1 n={len(pos)}   E_reg_neg_v1 n={len(neg)}\n")
    hdr = f"{'version':10} {'stratum':8} {'n':>6} {'judged rel':>11} {'metric':>8}"
    for name, rows, positive, metric in (
            ("recall@E_reg_pos", pos, True, "recall"),
            ("specificity@E_reg_neg", neg, False, "spec."),):
        print(f"=== {name} ===")
        print(hdr)
        for v in VERSIONS:
            s = score(rows, gates.get(v, {}), positive)
            for st, d in s.items():
                r = "n/a" if d.get("rate") is None else f"{d['rate']:.1%}"
                print(f"{v:10} {st:8} {d['n']:6} {d.get('hit','-'):>11} {r:>8}"
                      + (f"   (未标注 {d['missing']})" if d["missing"] else ""))
        print()

    with db.connect(autocommit=True) as c:
        print("=== 全语料 relevant ===")
        for r in c.execute("""select label_version, count(*) n,
            count(*) filter (where is_relevant) rel from post_labels
            where label_version = any(%s) group by 1 order by 1""", (VERSIONS,)):
            print(f"  {r['label_version']:10} 标注 {r['n']:6}  relevant {r['rel']:5}")

        print("\n=== llm_v2 判 relevant 而 rules_v2 判否:被 rules 哪一环挡的 ===")
        for r in c.execute("""
            select b.is_cyber, b.is_canada, count(*) n
            from post_labels a join post_labels b using (post_id)
            where a.label_version='llm_v2' and a.is_relevant
              and b.label_version='rules_v2' and not b.is_relevant
            group by 1,2 order by 3 desc"""):
            print(f"  is_cyber={str(r['is_cyber']):5} is_canada={str(r['is_canada']):5}"
                  f"  {r['n']:5}")

        print("\n=== rules_v2 判 relevant 而 llm_v2 判否 ===")
        n = c.execute("""select count(*) n from post_labels a join post_labels b
            using (post_id) where a.label_version='llm_v2' and not a.is_relevant
            and b.label_version='rules_v2' and b.is_relevant""").fetchall()[0]["n"]
        print(f"  {n}")


if __name__ == "__main__":
    main()
