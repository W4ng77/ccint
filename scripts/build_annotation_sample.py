"""人工标注样本(SPEC §23–§24)。只用 CPU,不调任何模型。

    python scripts/build_annotation_sample.py [--n 500] [--reliability 150]

输出(均含正文与 handle,不进仓 —— 同 eval/ 的约定):
  outputs/interaction_experiment/annotation/annotation_items.csv  给标注员:只有上下文与正文,
      **不含**任何系统判定(actor 启发式、topic、feed 标记、计数)
  outputs/interaction_experiment/annotation/annotation_key.csv    item_id → 单元 URI 与全部分层字段
  outputs/interaction_experiment/annotation/sample_meta.json

标注单元:种子帖 / 深度 1 回复 / 深度 2 回复 / 引用帖。转发无文本,不标。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from ccint import db
from ccint.interaction import manifest as mf
from ccint.interaction.collect import EP_QUOTES, EP_THREAD

OUT = Path("outputs/interaction_experiment/annotation")
RUNS = Path("outputs/interaction_experiment/collection/full_runs.json")
ORACLE = Path("reports/oracle_events.json")
RNG_SEED = 20260926
# 单元类型配额(按 n=500 的比例);不足时余量顺延给其他类型
SHARE = {"seed": 0.40, "reply_d1": 0.30, "reply_d2": 0.14, "quote": 0.16}
ITEM_COLS = ["item_id", "unit_type", "context_root", "context_parent", "text",
             "author_handle", "author_display_name", "post_url",
             "role", "function", "confidence", "notes"]


def url(uri: str, handle: str | None) -> str:
    did, rkey = uri.split("/")[2], uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle or did}/post/{rkey}"


def group_of(r):
    if r["in_main"] == "true":
        return "main"
    return "legacy_only" if r["in_legacy"] == "true" else "rules_only"


def load_units(runs: list[int]) -> tuple[list[dict], dict]:
    m = mf.load("v2")
    seeds = {int(r["post_id"]): r for r in m.rows}
    oracle_uris = {h["uri"] for o in json.loads(ORACLE.read_text()) for h in (o.get("hits") or [])}
    with db.connect() as c:
        info = {r["post_id"]: r for r in c.execute("""
            SELECT p.post_id, p.source_post_id AS uri, p.author_handle, p.text, p.published_at,
                   p.raw_payload->'author'->>'displayName' AS display_name,
                   p.raw_payload->'record'->'reply'->'root'->>'uri' AS root_uri,
                   coalesce((p.raw_payload->>'replyCount')::int,0) + coalesce((p.raw_payload->>'quoteCount')::int,0)
                   + coalesce((p.raw_payload->>'repostCount')::int,0) + coalesce((p.raw_payload->>'likeCount')::int,0) AS eng,
                   coalesce(t.topic_key, CASE WHEN t.post_id IS NULL THEN 'no_topic_v2_row' ELSE 'unassigned' END) AS topic,
                   coalesce(a.actor_type, 'no_profile') AS actor_type,
                   EXISTS (SELECT 1 FROM post_cves pc WHERE pc.post_id = p.post_id) AS has_cve
            FROM posts p
            LEFT JOIN post_labels t ON t.post_id = p.post_id AND t.label_version = 'topic_v2'
            LEFT JOIN author_profiles a ON a.author_id = p.author_id AND a.profile_version = 'actor_v1'
            WHERE p.post_id = ANY(%s)""", (list(seeds),))}
        known_text = {r["uri"]: r["text"] for r in c.execute(
            "SELECT source_post_id AS uri, text FROM posts WHERE source_post_id = ANY(%s)",
            ([i["root_uri"] for i in info.values() if i["root_uri"]],))}
        # 每个种子取最近一次成功的线程 / 引用快照
        snaps = {}
        for s in c.execute("""
                SELECT seed_post_id, endpoint, raw_payload, page_number FROM interaction_snapshots
                WHERE collection_run_id = ANY(%s) AND success AND endpoint = ANY(%s)
                ORDER BY collection_run_id, page_number""", (runs, [EP_THREAD, EP_QUOTES])):
            key = (s["seed_post_id"], s["endpoint"])
            if s["page_number"] == 1:
                snaps[key] = []
            snaps.setdefault(key, []).append(s["raw_payload"])

    eng_median = sorted(i["eng"] for i in info.values())[len(info) // 2]
    t_median = sorted(i["published_at"] for i in info.values())[len(info) // 2]
    units = []

    def strata(sid):
        i = info[sid]
        return {"seed_post_id": sid, "group": group_of(seeds[sid]), "topic": i["topic"],
                "seed_actor_type": i["actor_type"], "has_cve": i["has_cve"],
                "event_linked": i["uri"] in oracle_uris,
                "engagement": "high" if i["eng"] > eng_median else "low",
                "half": "early" if i["published_at"] < t_median else "late"}

    for sid, i in info.items():
        observed = (sid, EP_THREAD) in snaps
        root_txt = known_text.get(i["root_uri"], "(root 不在语料中,见链接)") if i["root_uri"] else ""
        units.append({"unit_type": "seed", "uri": i["uri"], "context_root": root_txt,
                      "context_parent": "", "text": i["text"], "author_handle": i["author_handle"],
                      "author_display_name": i["display_name"] or "", "observed": observed,
                      **strata(sid)})
        for page in snaps.get((sid, EP_THREAD), []):
            t = page.get("thread") or {}
            if not t.get("$type", "").endswith("#threadViewPost"):
                continue
            seed_txt = (t["post"].get("record") or {}).get("text", "")
            for r1 in t.get("replies") or []:
                if not r1.get("$type", "").endswith("#threadViewPost"):
                    continue
                p1 = r1["post"]
                txt1 = (p1.get("record") or {}).get("text", "")
                units.append(_reply_unit("reply_d1", p1, seed_txt, seed_txt, strata(sid)))
                for r2 in r1.get("replies") or []:
                    if r2.get("$type", "").endswith("#threadViewPost"):
                        units.append(_reply_unit("reply_d2", r2["post"], seed_txt, txt1, strata(sid)))
        for page in snaps.get((sid, EP_QUOTES), []):
            for q in page.get("posts") or []:
                units.append({"unit_type": "quote", "uri": q["uri"], "context_root": "",
                              "context_parent": i["text"], "text": (q.get("record") or {}).get("text", ""),
                              "author_handle": (q.get("author") or {}).get("handle"),
                              "author_display_name": (q.get("author") or {}).get("displayName") or "",
                              "observed": True, **strata(sid)})
    # 同一帖可能出现在两个种子的邻域里:按 URI 去重,保留第一次
    uniq = {}
    for u in units:
        uniq.setdefault(u["uri"], u)
    return list(uniq.values()), {"eng_median": eng_median, "time_median": str(t_median)}


def _reply_unit(kind, post, root_txt, parent_txt, st):
    a = post.get("author") or {}
    return {"unit_type": kind, "uri": post["uri"], "context_root": root_txt,
            "context_parent": parent_txt, "text": (post.get("record") or {}).get("text", ""),
            "author_handle": a.get("handle"), "author_display_name": a.get("displayName") or "",
            "observed": True, **st}


def round_robin(pool, k, rng, keys=("group", "topic", "half", "has_cve", "event_linked")):
    groups = defaultdict(list)
    for u in pool:
        groups[tuple(u[x] for x in keys)].append(u)
    for g in groups.values():
        rng.shuffle(g)
    order = sorted(groups)
    rng.shuffle(order)
    out = []
    while len(out) < k and any(groups.values()):
        for g in order:
            if groups[g] and len(out) < k:
                out.append(groups[g].pop())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--reliability", type=int, default=150)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    runs = json.loads(RUNS.read_text())
    units, medians = load_units(runs)
    rng = random.Random(RNG_SEED)
    by_type = defaultdict(list)
    for u in units:
        if u["unit_type"] != "seed" or u["observed"]:
            by_type[u["unit_type"]].append(u)
    quota = {t: round(a.n * s) for t, s in SHARE.items()}
    # 互动单元不足时,余量顺延给种子帖
    for t in ("reply_d2", "quote", "reply_d1"):
        short = quota[t] - len(by_type[t])
        if short > 0:
            quota[t] -= short
            quota["seed"] += short
    chosen = []
    for t, k in quota.items():
        pool = by_type[t]
        if t == "seed":   # 种子帖再按「有无对话邻域」对半,避免全是无人回应的广播
            conv_seeds = {u["seed_post_id"] for u in units if u["unit_type"] != "seed"}
            yes = [u for u in pool if u["seed_post_id"] in conv_seeds]
            no = [u for u in pool if u["seed_post_id"] not in conv_seeds]
            ky = min(len(yes), k // 2)
            chosen += round_robin(yes, ky, rng) + round_robin(no, k - ky, rng)
        else:
            chosen += round_robin(pool, k, rng)
    rng.shuffle(chosen)
    rel = set()
    for t in quota:   # 信度子集按单元类型等比例
        ids = [i for i, u in enumerate(chosen) if u["unit_type"] == t]
        rel.update(rng.sample(ids, min(len(ids), round(a.reliability * len(ids) / len(chosen)))))
    items, keys = [], []
    for i, u in enumerate(chosen):
        item_id = f"A{i + 1:04d}"
        items.append({"item_id": item_id, "unit_type": u["unit_type"],
                      "context_root": u["context_root"], "context_parent": u["context_parent"],
                      "text": u["text"], "author_handle": u["author_handle"] or "",
                      "author_display_name": u["author_display_name"],
                      "post_url": url(u["uri"], u["author_handle"]),
                      "role": "", "function": "", "confidence": "", "notes": ""})
        keys.append({"item_id": item_id, "unit_uri": u["uri"], "reliability_subset": i in rel,
                     **{k: u[k] for k in ("unit_type", "seed_post_id", "group", "topic",
                                          "seed_actor_type", "has_cve", "event_linked",
                                          "engagement", "half")}})
    with (OUT / "annotation_items.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ITEM_COLS)
        w.writeheader()
        w.writerows(items)
    with (OUT / "annotation_key.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(keys[0]))
        w.writeheader()
        w.writerows(keys)
    meta = {"rng_seed": RNG_SEED, "runs": runs, "n": len(items), "reliability_subset": len(rel),
            "available_units": {t: len(v) for t, v in by_type.items()}, "quota": quota,
            **medians,
            "strata": {k: dict(Counter(str(x[k]) for x in keys))
                       for k in ("unit_type", "group", "topic", "seed_actor_type", "has_cve",
                                 "event_linked", "engagement", "half")},
            "items_sha256": hashlib.sha256((OUT / "annotation_items.csv").read_bytes()).hexdigest()}
    (OUT / "sample_meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    print(json.dumps(meta, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
