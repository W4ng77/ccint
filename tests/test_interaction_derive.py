"""interaction 边派生的纯函数测试(不连库、不联网)。"""
from datetime import datetime, timezone

from ccint.interaction.collect import EP_QUOTES, EP_REPOSTS, EP_THREAD, classify_error, thread_gap
from ccint.interaction.derive import derive_snapshot

AT = datetime(2026, 9, 25, tzinfo=timezone.utc)
SEED = "at://did:plc:seed/app.bsky.feed.post/s"


def post(uri, did, parent=None, root=None, created="2026-09-01T00:00:00Z", **kw):
    rec = {"text": "x", "createdAt": created}
    if parent:
        rec["reply"] = {"parent": {"uri": parent}, "root": {"uri": root or parent}}
    return {"uri": uri, "author": {"did": did, "handle": did}, "record": rec,
            "indexedAt": "2026-09-01T00:00:01Z", **kw}


def tvp(p, replies=()):
    return {"$type": "app.bsky.feed.defs#threadViewPost", "post": p, "replies": list(replies)}


def snap(endpoint, data, success=True):
    return {"endpoint": endpoint, "raw_payload": data, "success": success,
            "seed_post_uri": SEED, "retrieved_at": AT}


def thread():
    r1 = post("at://a/p/1", "did:a", parent=SEED, root=SEED)
    r2 = post("at://b/p/2", "did:b", parent="at://a/p/1", root=SEED, replyCount=1)
    return {"thread": tvp(post(SEED, "did:plc:seed", replyCount=2), [
        tvp(r1, [tvp(r2)]),
        {"$type": "app.bsky.feed.defs#notFoundPost", "uri": "at://gone", "notFound": True},
    ])}


def test_thread_edges_depths_and_consistency():
    edges = derive_snapshot(snap(EP_THREAD, thread()), "did:plc:seed")
    replies = sorted((e for e in edges if e["edge_type"] == "reply"), key=lambda e: e["depth"])
    assert [(e["source_post_uri"], e["target_post_uri"], e["depth"]) for e in replies] == [
        ("at://a/p/1", SEED, 1), ("at://b/p/2", "at://a/p/1", 2)]
    assert replies[1]["target_actor_id"] == "did:a"
    assert all(e["metadata_json"]["parent_consistent"] for e in replies)
    assert all(e["metadata_json"]["root_consistent"] for e in replies)
    assert replies[1]["metadata_json"]["has_deeper"] is True
    # notFoundPost 不产生边;作者边 = 种子 + 两条回复
    assert sum(e["edge_type"] == "author" for e in edges) == 3


def test_derivation_is_deterministic():
    a = derive_snapshot(snap(EP_THREAD, thread()), "did:plc:seed")
    b = derive_snapshot(snap(EP_THREAD, thread()), "did:plc:seed")
    assert [e["edge_key"] for e in a] == [e["edge_key"] for e in b]


def test_repost_has_no_event_time():
    edges = derive_snapshot(snap(EP_REPOSTS, {"repostedBy": [{"did": "did:r"}]}), "did:plc:seed")
    assert len(edges) == 1 and edges[0]["event_created_at"] is None
    assert edges[0]["source_post_uri"] is None and edges[0]["source_actor_id"] == "did:r"


def test_quote_checks_embed_target():
    q = post("at://q/p/1", "did:q")
    q["record"]["embed"] = {"$type": "app.bsky.embed.record", "record": {"uri": SEED}}
    edges = derive_snapshot(snap(EP_QUOTES, {"posts": [q]}), "did:plc:seed")
    quote = [e for e in edges if e["edge_type"] == "quote"][0]
    assert quote["metadata_json"]["quoted_uri_consistent"] is True
    assert quote["event_created_at"] is not None


def test_failed_snapshot_yields_no_edges():
    assert derive_snapshot(snap(EP_THREAD, None, success=False), "did:x") == []


def test_suspect_created_at_is_flagged_not_replaced():
    r = post("at://a/p/1", "did:a", parent=SEED, created="2016-01-01T00:00:00Z")
    data = {"thread": tvp(post(SEED, "did:plc:seed"), [tvp(r)])}
    e = [e for e in derive_snapshot(snap(EP_THREAD, data), "x") if e["edge_type"] == "reply"][0]
    assert e["metadata_json"]["created_at_suspect"] is True
    assert e["event_created_at"].year == 2016


def test_thread_gap_and_error_classes():
    g = thread_gap(thread())
    assert (g["reply_count"], g["returned_depth1"], g["returned_depth2"]) == (2, 2, 1)
    assert g["node_types"] == {"threadViewPost": 2, "notFoundPost": 1}
    assert classify_error(400, {"error": "NotFound"}) == "not_found"
    assert classify_error(None, None) == "transport"
    assert classify_error(502, None) == "server_error"
