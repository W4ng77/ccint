"""用外部登记表构造弱监督评测集(T0)。

**设计约束 1:评测集的定义里不得出现被评测系统的任何输出。**
否则该系统在这个集上的指标是构造出来的。一个具体的反例:若负例定义为
「命中外国受害组织 ∧ rules_v2.is_canada = false」,则 rules_v2 在该集上的
precision 恒等于 1,而任何新判定器只会更差 —— 那个「更差」是评测集构造的
产物,与判定器好坏无关。所以这里负例只用 registry 侧的条件。

**设计约束 2:正例必须按账号类型分层报告。**
勒索软件登记表的受害组织名,在社交平台上主要出现在**转发该登记表的自动
情报 feed** 里 —— 实测命中帖 86% 来自 intel_feed 类账号,榜首之一就是
ransomware.live 自己的账号,零赞率 94.9%。不分层的 recall 奖励的是「抓到
bot」,而这个项目的研究对象是有机讨论。分层之后,非 feed 层的样本量本身
就是一个结论。

**已知偏差(必须随指标一起报告):** 登记源只覆盖勒索软件泄露站公开点名的
受害者。数据泄露、钓鱼、政策类事件它看不到;付了赎金没被挂站的也看不到。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from collections import Counter

from psycopg.types.json import Jsonb

from ccint import db

# 太短或单词的组织名会把语料匹爆("Storm" / "MEQ")。要求归一化后 >= 8 字符
# 且至少 2 个 token,或是域名。这会牺牲一部分真实事件,牺牲量在报告里给出。
MIN_CANON_LEN = 8


def usable(canon: str, etype: str) -> bool:
    if etype == "domain":
        return "." in canon and len(canon) >= 7
    return len(canon) >= MIN_CANON_LEN and len(canon.split()) >= 2


def load_entities(countries: list[str]) -> dict[str, list[dict]]:
    q = """
    SELECT e.canonical, e.entity_text, e.entity_type, v.event_id, v.country,
           v.published_at_utc, v.title
    FROM external_event_entities e JOIN external_events v USING (event_id)
    WHERE v.country = ANY(%s)
    """
    out: dict[str, list[dict]] = {}
    with db.connect(autocommit=True) as c:
        for r in c.execute(q, (countries,)):
            if usable(r["canonical"], r["entity_type"]):
                out.setdefault(r["canonical"], []).append(dict(r))
    return out


def author_strata() -> dict[str, str]:
    """全语料行为式分层:高产 + 近乎零互动 = 自动情报 feed。

    不用 author_profiles,因为它只覆盖 relevant 集里的 372 个作者,而评测集
    要覆盖全语料。判据与项目已有的 broadcast 判据同族,但这里只做分层标签,
    不做剔除。
    """
    q = """
    SELECT author_id,
           count(*) n,
           avg(coalesce(like_count,0) + coalesce(repost_count,0)) eng
    FROM posts GROUP BY 1
    """
    out = {}
    with db.connect(autocommit=True) as c:
        for r in c.execute(q):
            out[r["author_id"]] = ("feed" if r["n"] >= 50 and (r["eng"] or 0) < 0.5
                                   else "other")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos-set", default="E_reg_pos_v1")
    ap.add_argument("--neg-set", default="E_reg_neg_v1")
    ap.add_argument("--window-days", type=int, default=30,
                    help="正例要求帖子发布时间落在事件公布日的 [-2, +N] 天内")
    a = ap.parse_args()

    ca = load_entities(["CA"])
    foreign = load_entities(["US", "FR", "GB"])
    overlap = set(ca) & set(foreign)
    for k in overlap:
        foreign.pop(k, None)          # 同名跨国组织不进负例
    print(f"CA 实体 {len(ca)}  外国实体 {len(foreign)}(去掉与 CA 同名 {len(overlap)})")

    pats_ca = {k: re.compile(r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b", re.I)
               for k in ca}
    pats_fo = {k: re.compile(r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b", re.I)
               for k in foreign}

    strata = author_strata()
    with db.connect(autocommit=True) as c:
        posts = c.execute(
            "SELECT post_id, author_id, published_at, text FROM posts").fetchall()

    pos, neg, out_of_window = [], [], 0
    for p in posts:
        t = p["text"] or ""
        hit_ca = [k for k, pat in pats_ca.items() if pat.search(t)]
        hit_fo = [k for k, pat in pats_fo.items() if pat.search(t)]
        if hit_ca:
            evs = [e for k in hit_ca for e in ca[k]]
            deltas = [(p["published_at"] - e["published_at_utc"]).days for e in evs]
            near = [d for d in deltas if -2 <= d <= a.window_days]
            if not near:
                out_of_window += 1
                continue
            pos.append((p, hit_ca, evs, min(near, key=abs)))
        elif hit_fo:
            neg.append((p, hit_fo))

    with db.connect(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM eval_sets WHERE eval_set_id = ANY(%s)",
                    ([a.pos_set, a.neg_set],))
        for p, hits, evs, d in pos:
            cur.execute(
                "INSERT INTO eval_sets (eval_set_id, post_id, label, source, "
                "stratum, evidence) VALUES (%s,%s,%s,%s,%s,%s)",
                (a.pos_set, p["post_id"], True, "ransomware.live:CA",
                 strata.get(p["author_id"], "other"),
                 Jsonb({"entities": hits, "days_from_event": d,
                        "event_ids": sorted({e["event_id"] for e in evs})[:5]})))
        for p, hits in neg:
            cur.execute(
                "INSERT INTO eval_sets (eval_set_id, post_id, label, source, "
                "stratum, evidence) VALUES (%s,%s,%s,%s,%s,%s)",
                (a.neg_set, p["post_id"], False, "ransomware.live:US/FR/GB",
                 strata.get(p["author_id"], "other"), Jsonb({"entities": hits})))
        conn.commit()

    print(f"\n{a.pos_set}: {len(pos)} 帖  (窗口外剔除 {out_of_window})")
    print("  分层:", dict(Counter(strata.get(p["author_id"], "other")
                                   for p, *_ in pos)))
    print(f"{a.neg_set}: {len(neg)} 帖")
    print("  分层:", dict(Counter(strata.get(p["author_id"], "other")
                                   for p, _ in neg)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
