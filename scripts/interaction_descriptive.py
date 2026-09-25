"""完整邻域采集的描述性分析(SPEC §18–§21、§41 部分、表 1/2/5 中不需要人工标注的列)。

只描述,不检验趋势,不做因果解读。只用 CPU。

    python scripts/interaction_descriptive.py
    → outputs/interaction_experiment/descriptive/descriptive.json

口径:
- 每个种子取所有 full run 中**最近一次**非 failed 的结局;全部 failed 的种子记为 failed。
- observed = 线程与引用两个 endpoint 均为 success/partial(与试点报告一致)。
- CSR 分子:深度 ≤ 2 有回复或有引用;不计转发(SPEC §12)。深度 1 为空 ⟹ 深度 2 必为空,
  故 d=1 与 d=2 的 CSR 相同,深度敏感性体现在回复量与参与者上。
- topic 只用 `topic_v2`:NULL 记 `unassigned`,无 topic_v2 行(rules_v2 独有种子)记 `no_topic_v2_row`;
  不用 rules_v2 的 topic 回填 —— 两套 topic 词表不同,混用会把版本差异当成 topic 差异。
- 参与者 = 回复作者 ∪ 引用作者 ∪ 转发者;种子作者单列。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from ccint import db
from ccint.interaction import manifest as mf
from ccint.interaction.collect import EP_QUOTES, EP_REPOSTS, EP_THREAD
from ccint.interaction.derive import DERIVATION_VERSION

OUT = Path("outputs/interaction_experiment/descriptive")
RUNS = Path("outputs/interaction_experiment/collection/full_runs.json")
POPS = {"collect": "in_collect", "main": "in_main", "legacy": "in_legacy", "rules": "in_rules"}
OK = {"success", "partial"}


def dist(xs) -> dict:
    a = np.asarray(list(xs), dtype=float)
    if a.size == 0:
        return {"n": 0}
    q1, med, q3 = np.percentile(a, [25, 50, 75])
    return {"n": int(a.size), "sum": int(a.sum()), "mean": round(float(a.mean()), 3),
            "median": float(med), "iqr": [float(q1), float(q3)], "max": int(a.max()),
            "zero_share": round(float((a == 0).mean()), 3)}


def concentration(counts: Counter) -> dict:
    tot = sum(counts.values())
    if not tot:
        return {"total": 0}
    v = sorted(counts.values(), reverse=True)
    s = np.asarray(v, dtype=float) / tot
    return {"total": tot, "actors": len(v), "hhi": round(float((s ** 2).sum()), 4),
            "top1": round(float(s[:1].sum()), 3), "top5": round(float(s[:5].sum()), 3),
            "top10": round(float(s[:10].sum()), 3)}


def load():
    m = mf.load("v2")
    runs = json.loads(RUNS.read_text())
    seeds = {int(r["post_id"]): r for r in m.rows}
    with db.connect() as c:
        oc = defaultdict(dict)
        for r in c.execute("""
                SELECT collection_run_id, seed_post_id, endpoint, outcome, detail
                FROM interaction_seed_outcomes WHERE collection_run_id = ANY(%s)
                ORDER BY collection_run_id""", (runs,)):
            prev = oc[r["seed_post_id"]].get(r["endpoint"])
            if prev is None or r["outcome"] != "failed":      # 后来的非 failed 覆盖先前
                oc[r["seed_post_id"]][r["endpoint"]] = r
        # 每个种子只用它被采用的那个 run 的边
        use_run = {s: v[EP_THREAD]["collection_run_id"] for s, v in oc.items()}
        edges = [e for e in c.execute("""
                SELECT collection_run_id, seed_post_id, edge_type, depth, source_post_uri,
                       source_actor_id, target_post_uri, event_created_at, metadata_json
                FROM interaction_edges
                WHERE collection_run_id = ANY(%s) AND derivation_version = %s""",
                (runs, DERIVATION_VERSION)) if use_run.get(e["seed_post_id"]) == e["collection_run_id"]]
        info = {r["post_id"]: r for r in c.execute("""
            SELECT p.post_id, p.author_id, p.published_at,
                   (p.raw_payload->>'replyCount')::int AS rc0,
                   (p.raw_payload->>'quoteCount')::int AS qc0, p.author_handle,
                   coalesce(t.topic_key, CASE WHEN t.post_id IS NULL THEN 'no_topic_v2_row' ELSE 'unassigned' END) AS topic,
                   coalesce(a.actor_type, 'no_profile') AS actor_type,
                   coalesce(a.function, 'no_profile') AS actor_function,
                   EXISTS (SELECT 1 FROM post_cves pc WHERE pc.post_id = p.post_id) AS has_cve
            FROM posts p
            LEFT JOIN post_labels t ON t.post_id = p.post_id AND t.label_version = 'topic_v2'
            LEFT JOIN author_profiles a ON a.author_id = p.author_id AND a.profile_version = 'actor_v1'
            WHERE p.post_id = ANY(%s)""", (list(seeds),))}
        thread_counts = {r["seed_post_id"]: r for r in c.execute("""
            SELECT seed_post_id, collection_run_id,
                   (raw_payload->'thread'->'post'->>'repostCount')::int AS rpc,
                   (raw_payload->'thread'->'post'->>'quoteCount')::int AS qc,
                   (raw_payload->'thread'->'post'->>'replyCount')::int AS rc
            FROM interaction_snapshots
            WHERE collection_run_id = ANY(%s) AND endpoint = %s AND success""",
            (runs, EP_THREAD)) if use_run.get(r["seed_post_id"]) == r["collection_run_id"]}
    return m, runs, seeds, oc, edges, info, thread_counts


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    m, runs, seeds, oc, edges, info, tc = load()
    ps = defaultdict(lambda: {"d1": 0, "d2": 0, "quote": 0, "reposter": 0,
                              "reply_actors": set(), "reply_actors_d1": set(),
                              "quote_actors": set(), "reposters": set(), "reply_delays_h": []})
    for e in edges:
        s = ps[e["seed_post_id"]]
        if e["edge_type"] == "reply":
            s["d1" if e["depth"] == 1 else "d2"] += 1
            s["reply_actors"].add(e["source_actor_id"])
            if e["depth"] == 1:
                s["reply_actors_d1"].add(e["source_actor_id"])
            if e["event_created_at"] is not None:
                s["reply_delays_h"].append(
                    (e["event_created_at"] - info[e["seed_post_id"]]["published_at"]).total_seconds() / 3600)
        elif e["edge_type"] == "quote":
            s["quote"] += 1
            s["quote_actors"].add(e["source_actor_id"])
        elif e["edge_type"] == "repost":
            s["reposter"] += 1
            s["reposters"].add(e["source_actor_id"])

    def status(sid):
        v = oc.get(sid)
        if not v:
            return "missing"
        t, q = v[EP_THREAD]["outcome"], v[EP_QUOTES]["outcome"]
        if t in OK and q in OK:
            return "observed"
        if "unavailable" in (t, q):
            return "unavailable"
        return "failed"

    st = {sid: status(sid) for sid in seeds}
    conv = lambda sid: ps[sid]["d1"] > 0 or ps[sid]["quote"] > 0

    def summarize(ids, depth=2):
        obs = [s for s in ids if st[s] == "observed"]
        rep = lambda s: ps[s]["d1"] + (ps[s]["d2"] if depth == 2 else 0)
        ractors = lambda s: ps[s]["reply_actors"] if depth == 2 else ps[s]["reply_actors_d1"]
        part = set().union(*[ractors(s) | ps[s]["quote_actors"] | ps[s]["reposters"] for s in obs]) if obs else set()
        part_nr = set().union(*[ractors(s) | ps[s]["quote_actors"] for s in obs]) if obs else set()
        authors = {info[s]["author_id"] for s in obs}
        return {
            "seeds": len(ids), "status": dict(Counter(st[s] for s in ids)),
            "observed": len(obs), "conversation_seeds": sum(conv(s) for s in obs),
            "CSR": round(sum(conv(s) for s in obs) / len(obs), 4) if obs else None,
            "reply_bearing_seeds": sum(ps[s]["d1"] > 0 for s in obs),
            "quote_bearing_seeds": sum(ps[s]["quote"] > 0 for s in obs),
            "repost_bearing_seeds": sum(ps[s]["reposter"] > 0 for s in obs),
            "depth1_replies": sum(ps[s]["d1"] for s in obs),
            "depth2_replies": sum(ps[s]["d2"] for s in obs) if depth == 2 else 0,
            "quotes": sum(ps[s]["quote"] for s in obs),
            "reposter_edges": sum(ps[s]["reposter"] for s in obs),
            "unique_reposters": len(set().union(*[ps[s]["reposters"] for s in obs])) if obs else 0,
            "unique_seed_authors": len(authors),
            "unique_participants": len(part),
            "unique_participants_excl_reposters": len(part_nr),
            "unique_actors_all": len(part | authors),
            "dist": {"replies": dist(rep(s) for s in obs),
                     "depth1": dist(ps[s]["d1"] for s in obs),
                     "depth2": dist(ps[s]["d2"] for s in obs),
                     "quotes": dist(ps[s]["quote"] for s in obs),
                     "reposters": dist(ps[s]["reposter"] for s in obs),
                     "participants_per_seed": dist(len(ractors(s) | ps[s]["quote_actors"] | ps[s]["reposters"])
                                                   for s in obs)},
        }

    pops = {k: [s for s, r in seeds.items() if r[col] == "true"] for k, col in POPS.items()}
    pops["legacy_only_removed40"] = [s for s, r in seeds.items()
                                     if r["in_legacy"] == "true" and r["in_main"] == "false"]
    pops["rules_only"] = [s for s, r in seeds.items() if r["in_rules"] == "true" and r["in_legacy"] == "false"]
    rep: dict = {"manifest_sha256": m.sha256, "full_runs": runs, "derivation_version": DERIVATION_VERSION}
    rep["populations"] = {k: summarize(v) for k, v in pops.items()}
    rep["depth_sensitivity_main"] = {"d1": summarize(pops["main"], depth=1),
                                     "d2": summarize(pops["main"], depth=2)}
    main_obs = [s for s in pops["main"] if st[s] == "observed"]

    # §21 集中度(主人群)
    seed_prod = Counter(info[s]["author_id"] for s in pops["main"])
    reply_prod, quote_prod, repost_prod = Counter(), Counter(), Counter()
    for e in edges:
        if seeds[e["seed_post_id"]]["in_main"] != "true":
            continue
        {"reply": reply_prod, "quote": quote_prod, "repost": repost_prod}.get(
            e["edge_type"], Counter())[e["source_actor_id"]] += 1
    rep["concentration_main"] = {"seed_posts": concentration(seed_prod),
                                 "replies": concentration(reply_prod),
                                 "quotes": concentration(quote_prod),
                                 "reposts": concentration(repost_prod)}

    # §41 高产账号剔除(按种子帖产量排名)
    ranked = [a for a, _ in seed_prod.most_common()]
    rep["top_actor_removal_main"] = {}
    for k in (0, 1, 5, 10):
        drop = set(ranked[:k])
        ids = [s for s in pops["main"] if info[s]["author_id"] not in drop]
        x = summarize(ids)
        rep["top_actor_removal_main"][f"minus_top{k}"] = {
            "removed_actor_ids": ranked[:k],
            "seeds": x["seeds"], "observed": x["observed"], "CSR": x["CSR"],
            "replies": x["depth1_replies"] + x["depth2_replies"], "quotes": x["quotes"],
            "unique_participants": x["unique_participants"]}
    handle = {info[s]["author_id"]: info[s]["author_handle"] for s in pops["main"]}
    rep["top_seed_authors_main"] = [
        {"author_id": a, "handle": handle[a], "seed_posts": n,
         "actor_type": next(info[s]["actor_type"] for s in pops["main"] if info[s]["author_id"] == a)}
        for a, n in seed_prod.most_common(10)]

    # 按 topic(表 5 的非人工列)与按作者启发式类型
    def by(key):
        groups = defaultdict(list)
        for s in pops["main"]:
            groups[info[s][key]].append(s)
        return {g: {k: v for k, v in summarize(ids).items() if k != "dist"}
                for g, ids in sorted(groups.items(), key=lambda kv: -len(kv[1]))}
    rep["by_topic_main"] = by("topic")
    rep["by_actor_type_main"] = by("actor_type")
    rep["by_actor_function_main"] = by("actor_function")
    rep["by_cve_main"] = by("has_cve")

    # 覆盖缺口:自报计数 vs 实际取回
    gap = {"repost": [0, 0, 0], "quote": [0, 0, 0], "reply_d1": [0, 0, 0]}
    for s in main_obs:
        t = tc.get(s)
        if not t:
            continue
        for k, want, got in (("repost", t["rpc"], ps[s]["reposter"]), ("quote", t["qc"], ps[s]["quote"]),
                             ("reply_d1", t["rc"], ps[s]["d1"])):
            want = want or 0
            gap[k][0] += want
            gap[k][1] += got
            gap[k][2] += got < want
    rep["count_gap_main"] = {k: {"self_reported": a, "retrieved": b, "seeds_short": c}
                             for k, (a, b, c) in gap.items()}
    # 原始采集时刻的计数快照(replyCount 或 quoteCount > 0)对「有对话」的预测
    tab = Counter((bool(info[s]["rc0"] or info[s]["qc0"]), conv(s)) for s in main_obs)
    rep["snapshot_counts_vs_conversation_main"] = {f"snapshot>0={a},conv={b}": n
                                                   for (a, b), n in sorted(tab.items())}
    delays = [d for s in main_obs for d in ps[s]["reply_delays_h"]]
    rep["reply_delay_hours_main"] = {**dist(delays), "p90": float(np.percentile(delays, 90)) if delays else None,
                                     "share_within_24h": round(float(np.mean(np.asarray(delays) <= 24)), 3) if delays else None}
    rep["thread_not_success"] = {
        f"{o}:{e}": n for (o, e), n in Counter(
            (v[EP_THREAD]["outcome"], (v[EP_THREAD]["detail"] or {}).get("error_type"))
            for v in oc.values() if v[EP_THREAD]["outcome"] != "success").items()}
    rep["endpoint_outcomes"] = {ep: dict(Counter(v[ep]["outcome"] for v in oc.values() if ep in v))
                                for ep in (EP_THREAD, EP_QUOTES, EP_REPOSTS)}
    # §28 前三种测量的 topic 排名对比(描述性;S_human / S_broadcast 待标注验证后才有)
    #   S_post        主人群种子帖数
    #   S_actor       种子作者 ∪ 回复/引用作者(不含转发者 —— 转发无时间,与 S_interaction 口径一致)
    #   S_interaction 带时间戳的回复(d≤2)+ 引用帖数
    from scipy.stats import spearmanr
    sens = defaultdict(lambda: {"post": 0, "actors": set(), "interaction": 0})
    for s in main_obs:
        k = info[s]["topic"]
        sens[k]["post"] += 1
        sens[k]["actors"] |= {info[s]["author_id"]} | ps[s]["reply_actors"] | ps[s]["quote_actors"]
        sens[k]["interaction"] += ps[s]["d1"] + ps[s]["d2"] + ps[s]["quote"]
    topics = sorted(sens, key=lambda k: -sens[k]["post"])
    rows = {k: {"S_post": sens[k]["post"], "S_actor": len(sens[k]["actors"]),
                "S_interaction": sens[k]["interaction"],
                "interaction_per_post": round(sens[k]["interaction"] / sens[k]["post"], 3)}
            for k in topics}
    for col in ("S_post", "S_actor", "S_interaction"):
        order = sorted(topics, key=lambda k: -rows[k][col])
        for r, k in enumerate(order, 1):
            rows[k][f"rank_{col}"] = r
    vec = lambda col: [rows[k][col] for k in topics]
    rho = lambda a, b: round(float(spearmanr(vec(a), vec(b)).statistic), 3)
    rep["sensors_by_topic_main"] = {
        "note": "observed main seeds only; totals over the whole window; descriptive, no trend test",
        "topics": rows,
        "spearman_topics": {"post_vs_actor": rho("S_post", "S_actor"),
                            "post_vs_interaction": rho("S_post", "S_interaction"),
                            "actor_vs_interaction": rho("S_actor", "S_interaction")},
        "n_topics": len(topics)}

    (OUT / "descriptive.json").write_text(json.dumps(rep, indent=1, default=str, ensure_ascii=False))
    print(json.dumps(rep, indent=1, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
