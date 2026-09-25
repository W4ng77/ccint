"""从 interaction_snapshots 派生 interaction_edges。见 SPEC §7.2、§8、§9。

[MUST] 确定性:同一快照 + 同一 DERIVATION_VERSION 永远产出同一组边。
[MUST] 只派生响应里明确给出的关系:不按文本相似度补边,不按时间邻近拼线程;
notFoundPost / blockedPost 节点只计数,不产生边。
[MUST] repost 的 event_created_at 恒为 NULL —— getRepostedBy 不给时间,
不以快照时间代替(SPEC §9)。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from ..collectors.bluesky import _parse_created_at
from .collect import EP_QUOTES, EP_REPOSTS, EP_THREAD

DERIVATION_VERSION = "edges_v1"

# 与 ingest.resolve_published_at 相同的容忍度:createdAt 客户端自填,仅做标记不改值
_BACKFILL_TOL, _FUTURE_TOL = timedelta(days=2), timedelta(hours=1)


def edge_key(edge_type: str, source: str | None, target: str | None) -> str:
    return hashlib.sha256(f"{edge_type}|{source}|{target}".encode()).hexdigest()[:32]


def _time(post: dict) -> tuple[datetime | None, dict]:
    rec = post.get("record") or {}
    meta = {"indexed_at": post.get("indexedAt")}
    created = rec.get("createdAt")
    if not created:
        return None, meta
    ts = _parse_created_at(created)
    if post.get("indexedAt"):
        idx = _parse_created_at(post["indexedAt"])
        if ts > idx + _FUTURE_TOL or ts < idx - _BACKFILL_TOL:
            meta["created_at_suspect"] = True
    return ts, meta


def _author(post: dict, observed_at, **kw) -> dict:
    return dict(edge_type="author", source_post_uri=None, target_post_uri=post["uri"],
                source_actor_id=(post.get("author") or {}).get("did"), target_actor_id=None,
                event_created_at=_time(post)[0], observed_at=observed_at, depth=None,
                root_post_uri=None, metadata_json={"handle": (post.get("author") or {}).get("handle")},
                **kw)


def edges_from_thread(data: dict, seed_uri: str, observed_at) -> list[dict]:
    t = data.get("thread") or {}
    if not t.get("$type", "").endswith("#threadViewPost"):
        return []
    seed = t["post"]
    seed_actor = (seed.get("author") or {}).get("did")
    seed_root = ((seed.get("record") or {}).get("reply") or {}).get("root", {}).get("uri") or seed_uri
    out = [_author(seed, observed_at)]

    def walk(node, parent_uri, parent_actor, depth):
        for r in node.get("replies") or []:
            if not r.get("$type", "").endswith("#threadViewPost"):
                continue
            p = r["post"]
            ref = (p.get("record") or {}).get("reply") or {}
            ts, meta = _time(p)
            meta.update({
                "declared_parent": (ref.get("parent") or {}).get("uri"),
                "declared_root": (ref.get("root") or {}).get("uri"),
                "parent_consistent": (ref.get("parent") or {}).get("uri") == parent_uri,
                "root_consistent": (ref.get("root") or {}).get("uri") == seed_root,
                "reply_count": p.get("replyCount"),
                "has_deeper": depth == 2 and bool(p.get("replyCount")),
            })
            out.append(dict(edge_type="reply", source_post_uri=p["uri"], target_post_uri=parent_uri,
                            source_actor_id=(p.get("author") or {}).get("did"),
                            target_actor_id=parent_actor, event_created_at=ts,
                            observed_at=observed_at, depth=depth, root_post_uri=seed_root,
                            metadata_json=meta))
            out.append(_author(p, observed_at))
            if depth < 2:
                walk(r, p["uri"], (p.get("author") or {}).get("did"), depth + 1)

    walk(t, seed_uri, seed_actor, 1)
    return out


def edges_from_quotes(data: dict, seed_uri: str, seed_actor: str | None, observed_at) -> list[dict]:
    out = []
    for q in data.get("posts") or []:
        emb = (q.get("record") or {}).get("embed") or {}
        quoted = (emb.get("record") or {}).get("uri") or \
                 ((emb.get("record") or {}).get("record") or {}).get("uri")
        ts, meta = _time(q)
        meta.update({"embed_type": emb.get("$type"), "quoted_uri_consistent": quoted == seed_uri})
        out.append(dict(edge_type="quote", source_post_uri=q["uri"], target_post_uri=seed_uri,
                        source_actor_id=(q.get("author") or {}).get("did"),
                        target_actor_id=seed_actor, event_created_at=ts, observed_at=observed_at,
                        depth=None, root_post_uri=None, metadata_json=meta))
        out.append(_author(q, observed_at))
    return out


def edges_from_reposts(data: dict, seed_uri: str, seed_actor: str | None, observed_at) -> list[dict]:
    return [dict(edge_type="repost", source_post_uri=None, target_post_uri=seed_uri,
                 source_actor_id=a.get("did"), target_actor_id=seed_actor,
                 event_created_at=None, observed_at=observed_at, depth=None, root_post_uri=None,
                 metadata_json={"handle": a.get("handle")})
            for a in data.get("repostedBy") or []]


def derive_snapshot(snap: dict, seed_actor: str | None) -> list[dict]:
    """单个快照 → 边列表(不含库字段)。失败快照不产生边。"""
    if not snap["success"] or snap["raw_payload"] is None:
        return []
    data, uri, at = snap["raw_payload"], snap["seed_post_uri"], snap["retrieved_at"]
    if snap["endpoint"] == EP_THREAD:
        edges = edges_from_thread(data, uri, at)
    elif snap["endpoint"] == EP_QUOTES:
        edges = edges_from_quotes(data, uri, seed_actor, at)
    elif snap["endpoint"] == EP_REPOSTS:
        edges = edges_from_reposts(data, uri, seed_actor, at)
    else:
        raise ValueError(snap["endpoint"])
    for e in edges:
        src = e["source_post_uri"] or e["source_actor_id"]
        e["edge_key"] = edge_key(e["edge_type"], src, e["target_post_uri"])
    # 同一快照内同一关系只记一次(例如种子作者边不会重复,但防御性去重)
    uniq = {}
    for e in edges:
        uniq.setdefault(e["edge_key"], e)
    return list(uniq.values())


def derive_run(conn, run_id: int, commit: str) -> int:
    """为一次 run 的全部快照派生边。幂等:已派生的 (snapshot, edge_key, version) 跳过。"""
    snaps = conn.execute("""
        SELECT s.snapshot_id, s.collection_run_id, s.seed_post_id, s.seed_post_uri, s.endpoint,
               s.success, s.raw_payload, s.retrieved_at, p.author_id AS seed_actor
        FROM interaction_snapshots s JOIN posts p ON p.post_id = s.seed_post_id
        WHERE s.collection_run_id = %s ORDER BY s.snapshot_id""", (run_id,)).fetchall()
    n = 0
    with conn.cursor() as cur:
        for s in snaps:
            for e in derive_snapshot(s, s["seed_actor"]):
                cur.execute("""
                    INSERT INTO interaction_edges
                      (snapshot_id, collection_run_id, seed_post_id, edge_key, edge_type,
                       root_post_uri, source_post_uri, target_post_uri, source_actor_id,
                       target_actor_id, event_created_at, observed_at, depth,
                       derivation_version, code_commit, metadata_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (snapshot_id, edge_key, derivation_version) DO NOTHING""",
                    (s["snapshot_id"], s["collection_run_id"], s["seed_post_id"], e["edge_key"],
                     e["edge_type"], e["root_post_uri"], e["source_post_uri"],
                     e["target_post_uri"], e["source_actor_id"], e["target_actor_id"],
                     e["event_created_at"], e["observed_at"], e["depth"], DERIVATION_VERSION,
                     commit, Jsonb(e["metadata_json"])))
                n += cur.rowcount
    conn.execute("UPDATE neighborhood_runs SET edge_count = (SELECT count(*) FROM interaction_edges "
                 "WHERE collection_run_id = %s) WHERE run_id = %s", (run_id, run_id))
    return n
