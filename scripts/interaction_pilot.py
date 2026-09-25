"""邻域采集试点(SPEC §11–§12)。基础设施 QA,不做科学推断。

    python scripts/interaction_pilot.py collect   # 抽样 + 三个 run + 派生边
    python scripts/interaction_pilot.py report    # 从库里算 QA 指标 → JSON

三个 run:
  pilot             分层抽 100 个种子,page_limit=100
  pilot_repeat      其中 10 个再采一次:验证稳定身份去重 + 快照溯源分开
  pilot_pagination  互动最多的 5 个用 page_limit=2 再采:验证翻页结果与大页一致
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ccint import db
from ccint.interaction import manifest as mf
from ccint.interaction.collect import EP_QUOTES, EP_REPOSTS, EP_THREAD, git_commit, run
from ccint.interaction.derive import DERIVATION_VERSION, derive_run, derive_snapshot

OUT = Path("outputs/interaction_experiment/collection")
RUNS = OUT / "pilot_runs.json"
RNG_SEED = 20260925
ALLOC = {"main": 65, "legacy_only": 15, "rules_only": 20}
MAIN_CELLS = {(True, True): 8, (True, False): 20, (False, True): 5}   # 其余进 (False, False)


def group_of(r):
    if r["in_main"] == "true":
        return "main"
    return "legacy_only" if r["in_legacy"] == "true" else "rules_only"


def features(m: mf.Manifest) -> list[dict]:
    """抽样特征:采集时刻的计数快照、topic、actor 启发式。只用于分层,不定义种子。"""
    ids = [int(r["post_id"]) for r in m.rows]
    with db.connect() as c:
        f = {r["post_id"]: r for r in c.execute("""
            SELECT p.post_id, p.published_at,
                   coalesce((p.raw_payload->>'replyCount')::int, 0) AS rc,
                   coalesce((p.raw_payload->>'quoteCount')::int, 0) AS qc,
                   coalesce((p.raw_payload->>'repostCount')::int, 0) AS rpc,
                   coalesce((p.raw_payload->>'likeCount')::int, 0) AS lc,
                   coalesce(t.topic_key, r2.topic_key, '?') AS topic,
                   coalesce(a.actor_type, 'no_profile') AS actor
            FROM posts p
            LEFT JOIN post_labels t ON t.post_id = p.post_id AND t.label_version = 'topic_v2'
            LEFT JOIN post_labels r2 ON r2.post_id = p.post_id AND r2.label_version = 'rules_v2'
            LEFT JOIN author_profiles a ON a.author_id = p.author_id AND a.profile_version = 'actor_v1'
            WHERE p.post_id = ANY(%s)""", (ids,)).fetchall()}
    rows = []
    median_t = sorted(f[i]["published_at"] for i in ids)[len(ids) // 2]
    for r in m.rows:
        x = f[int(r["post_id"])]
        rows.append({**r, "group": group_of(r), "rc": x["rc"], "qc": x["qc"], "rpc": x["rpc"],
                     "engagement": x["rc"] + x["qc"] + x["rpc"] + x["lc"],
                     "topic": x["topic"], "actor": x["actor"],
                     "half": "early" if x["published_at"] < median_t else "late"})
    return rows


def round_robin(pool: list[dict], k: int, rng: random.Random) -> list[dict]:
    """按 (topic, half) 分组轮流抽,让小样本也铺开 topic 和时间。"""
    groups = defaultdict(list)
    for r in pool:
        groups[(r["topic"], r["half"])].append(r)
    for g in groups.values():
        rng.shuffle(g)
    keys = sorted(groups)
    rng.shuffle(keys)
    out = []
    while len(out) < k and any(groups.values()):
        for key in keys:
            if groups[key] and len(out) < k:
                out.append(groups[key].pop())
    return out


def sample(rows: list[dict]) -> list[dict]:
    rng = random.Random(RNG_SEED)
    chosen: dict[str, dict] = {}
    # 强制纳入:唯一一个 repost > 100 的种子(真实翻页),以及 replyCount 最高的 3 个(深度 2)
    forced = [max(rows, key=lambda r: r["rpc"])] + sorted(rows, key=lambda r: -r["rc"])[:3]
    for r in forced:
        chosen[r["post_id"]] = {**r, "why": "forced"}
    for grp, k in ALLOC.items():
        have = [r for r in chosen.values() if r["group"] == grp]
        pool = [r for r in rows if r["group"] == grp and r["post_id"] not in chosen]
        k -= len(have)
        if grp == "main":
            cells = defaultdict(list)
            for r in pool:
                cells[(r["rc"] > 0, r["qc"] > 0)].append(r)
            quota = dict(MAIN_CELLS)
            quota[(False, False)] = k - sum(quota.values())
            for cell, n in quota.items():
                for r in round_robin(cells[cell], n, rng):
                    chosen[r["post_id"]] = {**r, "why": f"cell reply={cell[0]} quote={cell[1]}"}
        else:
            yes = [r for r in pool if r["rc"] > 0 or r["qc"] > 0]
            no = [r for r in pool if not (r["rc"] > 0 or r["qc"] > 0)]
            ny = min(len(yes), k // 2)
            for r in round_robin(yes, ny, rng) + round_robin(no, k - ny, rng):
                chosen[r["post_id"]] = {**r, "why": "interaction=" + str(r in yes)}
    return sorted(chosen.values(), key=lambda r: int(r["post_id"]))


def cmd_collect() -> None:
    if RUNS.exists():
        sys.exit(f"{RUNS} 已存在:试点已采过。重复采集请先确认意图。")
    OUT.mkdir(parents=True, exist_ok=True)
    m = mf.load("v2")
    pilot = sample(features(m))
    print("pilot:", Counter(r["group"] for r in pilot))
    (OUT / "pilot_sample.json").write_text(json.dumps(
        [{k: r[k] for k in ("post_id", "post_uri", "group", "rc", "qc", "rpc", "engagement",
                            "topic", "actor", "half", "why")} for r in pilot], indent=1))
    runs = {"pilot": run(m, pilot, purpose="pilot")}
    top = sorted(pilot, key=lambda r: -(r["rc"] + r["qc"] + r["rpc"]))
    runs["pilot_repeat"] = run(m, top[:10], purpose="pilot_repeat")
    runs["pilot_pagination"] = run(m, top[:5], purpose="pilot_pagination", page_limit=2)
    commit = git_commit()
    with db.transaction() as conn:
        for rid in runs.values():
            derive_run(conn, rid, commit)
    RUNS.write_text(json.dumps(runs, indent=1))
    print(runs)


def q(c, sql, *a):
    return c.execute(sql, a).fetchall()


def cmd_report() -> None:
    runs = json.loads(RUNS.read_text())
    m = mf.load("v2")
    sample_rows = {int(r["post_id"]): r for r in json.loads((OUT / "pilot_sample.json").read_text())}
    P, R, G = runs["pilot"], runs["pilot_repeat"], runs["pilot_pagination"]
    rep: dict = {"manifest": {**m.counts(), "sha256": m.sha256, "version": m.version},
                 "runs": runs, "derivation_version": DERIVATION_VERSION}
    with db.connect() as c:
        # ---- run 级
        rep["run_stats"] = {row["purpose"]: row for row in q(c, """
            SELECT n.purpose, n.requested_seed_count, n.request_count, n.success_seed_count,
                   n.partial_seed_count, n.failed_seed_count, n.unavailable_seed_count,
                   n.retry_count, n.rate_limit_count, n.snapshot_count, n.edge_count,
                   r.status, extract(epoch FROM r.ended_at - r.started_at) AS runtime_s
            FROM neighborhood_runs n JOIN collection_runs r USING (run_id)
            WHERE n.run_id = ANY(%s)""", list(runs.values()))}
        # ---- 每种子结局
        oc = defaultdict(dict)
        for r in q(c, "SELECT seed_post_id, endpoint, outcome, detail FROM interaction_seed_outcomes "
                      "WHERE collection_run_id=%s", P):
            oc[r["seed_post_id"]][r["endpoint"]] = r
        rep["outcomes_by_endpoint"] = {ep: dict(Counter(v[ep]["outcome"] for v in oc.values()))
                                       for ep in (EP_THREAD, EP_QUOTES, EP_REPOSTS)}
        rep["partial_detail"] = {
            ep: [dict(seed=s, **v[ep]["detail"]) for s, v in oc.items()
                 if v[ep]["outcome"] == "partial"] for ep in (EP_THREAD, EP_QUOTES, EP_REPOSTS)}
        errs = q(c, """SELECT endpoint, error_type, http_status, page_number, count(*) n
                       FROM interaction_snapshots WHERE collection_run_id=%s AND NOT success
                       GROUP BY 1,2,3,4""", P)
        rep["error_snapshots"] = errs
        rep["failures"] = {
            "deleted_or_not_found_seeds": sum(1 for v in oc.values()
                                              if v[EP_THREAD]["outcome"] == "unavailable"
                                              and v[EP_THREAD]["detail"].get("error_type") == "not_found"),
            "unavailable_other": sum(1 for v in oc.values() if v[EP_THREAD]["outcome"] == "unavailable"
                                     and v[EP_THREAD]["detail"].get("error_type") != "not_found"),
            "pagination_failures": sum(1 for e in errs if e["page_number"] > 1),
            "other_api_failures": sum(e["n"] for e in errs if e["page_number"] == 1
                                      and e["error_type"] not in ("not_found", "blocked")),
        }
        # ---- 交互量(pilot run)
        E = q(c, """SELECT seed_post_id, edge_type, depth, source_post_uri, source_actor_id,
                           target_post_uri, event_created_at, metadata_json, edge_key
                    FROM interaction_edges WHERE collection_run_id=%s""", P)
        rp = [e for e in E if e["edge_type"] == "reply"]
        qt = [e for e in E if e["edge_type"] == "quote"]
        rs = [e for e in E if e["edge_type"] == "repost"]
        seed_authors = {e["source_actor_id"] for e in E if e["edge_type"] == "author"
                        and e["target_post_uri"] in {r["post_uri"] for r in sample_rows.values()}}
        participants = {e["source_actor_id"] for e in rp + qt + rs}
        rep["interaction"] = {
            "depth1_replies": sum(e["depth"] == 1 for e in rp),
            "depth2_replies": sum(e["depth"] == 2 for e in rp),
            "unique_replies": len({e["source_post_uri"] for e in rp}),
            "quotes": len({e["source_post_uri"] for e in qt}),
            "unique_reposters": len({e["source_actor_id"] for e in rs}),
            "unique_participating_actors_excl_seed_authors": len(participants),
            "unique_seed_authors": len(seed_authors),
            "unique_actors_incl_seed_authors": len(participants | seed_authors),
            "depth2_nodes_with_deeper_replies": sum(bool(e["metadata_json"].get("has_deeper")) for e in rp),
            "reply_created_at_suspect": sum(bool(e["metadata_json"].get("created_at_suspect")) for e in rp),
            "quote_created_at_suspect": sum(bool(e["metadata_json"].get("created_at_suspect")) for e in qt),
            "repost_edges_with_event_time": sum(e["event_created_at"] is not None for e in rs),
        }
        rep["consistency"] = {
            "reply_parent_consistent": f"{sum(e['metadata_json']['parent_consistent'] for e in rp)}/{len(rp)}",
            "reply_root_consistent": f"{sum(e['metadata_json']['root_consistent'] for e in rp)}/{len(rp)}",
            "quote_embed_consistent": f"{sum(e['metadata_json']['quoted_uri_consistent'] for e in qt)}/{len(qt)}",
        }
        # ---- CSR 与分组产出。observed = thread 与 quotes 均为 success/partial
        per = defaultdict(lambda: {"reply": 0, "quote": 0, "reposter": 0})
        for e in rp:
            per[e["seed_post_id"]]["reply"] += 1
        for e in qt:
            per[e["seed_post_id"]]["quote"] += 1
        for e in rs:
            per[e["seed_post_id"]]["reposter"] += 1
        ok = {"success", "partial"}

        def yield_of(seeds):
            obs = [s for s in seeds if oc[s][EP_THREAD]["outcome"] in ok
                   and oc[s][EP_QUOTES]["outcome"] in ok]
            conv = [s for s in obs if per[s]["reply"] or per[s]["quote"]]
            strict = [s for s in obs if oc[s][EP_THREAD]["outcome"] == "success"
                      and oc[s][EP_QUOTES]["outcome"] == "success"]
            return {"attempted": len(seeds), "observed": len(obs), "conversation_seeds": len(conv),
                    "CSR": round(len(conv) / len(obs), 3) if obs else None,
                    "CSR_strict_success_only": round(
                        sum(1 for s in strict if per[s]["reply"] or per[s]["quote"]) / len(strict), 3)
                    if strict else None,
                    "replies": sum(per[s]["reply"] for s in obs),
                    "quotes": sum(per[s]["quote"] for s in obs),
                    "reposters": sum(per[s]["reposter"] for s in obs),
                    "zero_interaction_incl_reposts": sum(1 for s in obs if not any(per[s].values()))}

        seeds = list(oc)
        rep["coverage_all"] = yield_of(seeds)
        rep["yield_by_group"] = {g: yield_of([s for s in seeds if sample_rows[s]["group"] == g])
                                 for g in ALLOC}
        rep["sample_strata"] = {k: dict(Counter(r[k] for r in sample_rows.values()))
                                for k in ("group", "topic", "actor", "half", "why")}
        # 计数快照的陈旧度:原采集时的 replyCount vs 本次线程返回的 replyCount
        stale = []
        for s, v in oc.items():
            d = v[EP_THREAD]["detail"]
            if "reply_count" in d:
                stale.append(d["reply_count"] - sample_rows[s]["rc"])
        rep["reply_count_drift_since_original_collection"] = dict(Counter(
            "0" if x == 0 else ("+" if x > 0 else "-") for x in stale))

        # ---- QA:原始快照可逐字节核对
        bad_hash = bad_json = n_snap = 0
        for s in q(c, "SELECT raw_body, raw_payload, raw_payload_sha256 FROM interaction_snapshots "
                      "WHERE collection_run_id = ANY(%s) AND raw_body IS NOT NULL", list(runs.values())):
            n_snap += 1
            body = gzip.decompress(s["raw_body"])
            bad_hash += hashlib.sha256(body).hexdigest() != s["raw_payload_sha256"]
            bad_json += json.loads(body) != s["raw_payload"]
        rep["qa_raw_persistence"] = {"snapshots_checked": n_snap, "sha_mismatch": bad_hash,
                                     "json_mismatch": bad_json}
        # ---- QA:派生确定性(从快照重算 edge_key 集合,与库比对)
        mism = 0
        stored = defaultdict(set)
        for e in q(c, "SELECT snapshot_id, edge_key FROM interaction_edges WHERE collection_run_id = ANY(%s)",
                   list(runs.values())):
            stored[e["snapshot_id"]].add(e["edge_key"])
        snaps = q(c, """SELECT s.snapshot_id, s.seed_post_uri, s.endpoint, s.success, s.raw_payload,
                               s.retrieved_at, p.author_id AS seed_actor
                        FROM interaction_snapshots s JOIN posts p ON p.post_id = s.seed_post_id
                        WHERE s.collection_run_id = ANY(%s)""", list(runs.values()))
        for s in snaps:
            mism += {e["edge_key"] for e in derive_snapshot(s, s["seed_actor"])} != stored[s["snapshot_id"]]
        rep["qa_deterministic_derivation"] = {"snapshots": len(snaps), "mismatched": mism}

        # ---- QA:重复采集(稳定身份去重 + 快照溯源分开)
        def keys(run_id, types):
            return {(e["seed_post_id"], e["edge_key"]) for e in q(c, """
                SELECT seed_post_id, edge_key FROM interaction_edges
                WHERE collection_run_id=%s AND edge_type = ANY(%s)""", run_id, types)}
        rseeds = {r["seed_post_id"] for r in q(c, "SELECT DISTINCT seed_post_id FROM interaction_seed_outcomes "
                                                  "WHERE collection_run_id=%s", R)}
        types = ["reply", "quote", "repost", "author"]
        a = {k for k in keys(P, types) if k[0] in rseeds}
        b = keys(R, types)
        shared_snaps = q(c, """SELECT count(*) n FROM interaction_snapshots a JOIN interaction_snapshots b
                               ON a.snapshot_id = b.snapshot_id
                               WHERE a.collection_run_id=%s AND b.collection_run_id=%s""", P, R)[0]["n"]
        rep["qa_repeat_run"] = {"seeds": len(rseeds), "edge_keys_first": len(a), "edge_keys_repeat": len(b),
                                "only_first": len(a - b), "only_repeat": len(b - a),
                                "shared_snapshot_ids": shared_snaps}
        # ---- QA:小页长翻页 vs 大页
        gseeds = {r["seed_post_id"] for r in q(c, "SELECT DISTINCT seed_post_id FROM interaction_seed_outcomes "
                                                  "WHERE collection_run_id=%s", G)}
        a = {k for k in keys(P, ["quote", "repost"]) if k[0] in gseeds}
        b = keys(G, ["quote", "repost"])
        pages = q(c, """SELECT endpoint, max(page_number) mx, count(*) n FROM interaction_snapshots
                        WHERE collection_run_id=%s GROUP BY 1""", G)
        rep["qa_pagination"] = {"seeds": len(gseeds), "keys_limit100": len(a), "keys_limit2": len(b),
                                "only_limit100": len(a - b), "only_limit2": len(b - a), "pages": pages,
                                "real_multipage_in_pilot": q(c, """
                                    SELECT endpoint, seed_post_id, max(page_number) pages
                                    FROM interaction_snapshots WHERE collection_run_id=%s
                                    GROUP BY 1,2 HAVING max(page_number) > 1""", P)}
        # ---- QA:派生幂等(再跑一次 derive_run 应插入 0 行)—— 在事务里做完回滚
    with db.connect(autocommit=False) as conn:
        rep["qa_derive_idempotent_new_rows"] = derive_run(conn, P, git_commit())
        conn.rollback()
    st = rep["run_stats"]["pilot"]
    per_seed_req = st["request_count"] / st["requested_seed_count"]
    per_seed_s = float(st["runtime_s"]) / st["requested_seed_count"]
    rep["full_run_estimate"] = {"seeds": m.counts()["in_collect"],
                                "requests": round(per_seed_req * m.counts()["in_collect"]),
                                "runtime_min": round(per_seed_s * m.counts()["in_collect"] / 60, 1)}
    (OUT / "pilot_report.json").write_text(json.dumps(rep, indent=1, default=str, ensure_ascii=False))
    print(json.dumps(rep, indent=1, default=str, ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "report"])
    {"collect": cmd_collect, "report": cmd_report}[ap.parse_args().cmd]()
