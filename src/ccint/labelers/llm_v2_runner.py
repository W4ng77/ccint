"""``llm_v2`` 的全库执行器。

两阶段,顺序是测量设计而不是性能优化(见 llm_v2 模块 docstring):
  阶段 1  加拿大实体抽取,跑**全库** —— 得到整个语料的加拿大实体图。
  阶段 2  cyber 判定,只跑阶段 1 有实体的帖。

阶段 1 必须跑全库而不是只跑被拒帖:如果只跑被拒帖,得到的实体图就带着
rules_v2 的选择偏差,而这张图正要被用来评估 rules_v2。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from psycopg.types.json import Jsonb

from .. import db
from ..labeling import _UPSERT
from ..llm import client as llm
from ..registry.ransomwarelive import canonical
from .llm_v2 import (CANADA_SCHEMA, CYBER_SCHEMA, LABEL_VERSION, Verdict,
                     locate, to_label)

log = logging.getLogger(__name__)

_ENT_UPSERT = """
INSERT INTO post_entities (post_id, label_version, entity_text, entity_type,
                           canonical, char_start)
VALUES (%(post_id)s, %(label_version)s, %(entity_text)s, %(entity_type)s,
        %(canonical)s, %(char_start)s)
ON CONFLICT (post_id, label_version, entity_text) DO NOTHING
"""


def _fetch(only_new: bool, limit: int | None, label_version: str) -> list[dict]:
    sql = """
    SELECT p.post_id, p.text FROM posts p
    WHERE length(coalesce(p.text,'')) > 0
      AND (%(only_new)s = false OR NOT EXISTS (
            SELECT 1 FROM post_labels x
            WHERE x.post_id = p.post_id AND x.label_version = %(v)s))
    ORDER BY p.post_id
    """
    with db.connect(autocommit=True) as c:
        rows = [dict(r) for r in c.execute(
            sql, {"only_new": only_new, "v": label_version}).fetchall()]
    return rows[:limit] if limit else rows


def run(*, base_url: str, model: str, workers: int = 32, seed: int = 0,
        only_new: bool = True, limit: int | None = None, quant: str | None = None,
        progress: int = 10000, label_version: str = LABEL_VERSION,
        canada_prompt_version: int = 1) -> dict:
    p_ca = llm.load_prompt("gate/canada_entities", canada_prompt_version)
    p_cy = llm.load_prompt("gate/is_cyber", 1)
    rows = _fetch(only_new, limit, label_version)
    n = len(rows)
    log.warning("llm_v2: %d posts", n)
    lim = httpx.Limits(max_connections=workers + 4,
                       max_keepalive_connections=workers + 4)
    stats = {"n": n, "phase1_err": 0, "with_entities": 0,
             "phase2_err": 0, "relevant": 0, "n_entities": 0}
    t0 = time.perf_counter()
    verdicts: dict[int, Verdict] = {}

    with httpx.Client(limits=lim) as http, ThreadPoolExecutor(workers) as pool:
        # ---- 阶段 1:全库加拿大实体 ----
        def ca(r):
            c = llm.call(http, base_url=base_url, model=model, prompt=p_ca,
                         user_text=r["text"], schema=CANADA_SCHEMA,
                         max_tokens=256, seed=seed)
            return r, c
        for i, (r, c) in enumerate(pool.map(ca, rows), 1):
            if c.data is None:
                stats["phase1_err"] += 1
                ents = []
            else:
                ents = [e for e in c.data.get("entities", []) if e.get("text")]
            verdicts[r["post_id"]] = Verdict(None, None, ents, False)
            if ents:
                stats["with_entities"] += 1
                stats["n_entities"] += len(ents)
            if i % progress == 0:
                log.warning("  phase1 %d/%d  entities=%d  err=%d  (%.0f/s)",
                            i, n, stats["with_entities"], stats["phase1_err"],
                            i / (time.perf_counter() - t0))

        # ---- 阶段 2:只对有实体的帖判 cyber ----
        cand = [r for r in rows if verdicts[r["post_id"]].entities]
        log.warning("llm_v2: phase2 on %d posts with Canadian entities", len(cand))

        def cy(r):
            c = llm.call(http, base_url=base_url, model=model, prompt=p_cy,
                         user_text=r["text"], schema=CYBER_SCHEMA,
                         max_tokens=96, seed=seed)
            return r, c
        for r, c in pool.map(cy, cand):
            v = verdicts[r["post_id"]]
            if c.data is None:
                stats["phase2_err"] += 1
                continue
            v.is_cyber = bool(c.data.get("is_cyber"))
            v.cyber_evidence = (c.data.get("evidence") or "")[:80]
            v.canada_screened = True
            if v.is_relevant:
                stats["relevant"] += 1

    # ---- 落库 ----
    txt = {r["post_id"]: r["text"] for r in rows}
    with db.connect(autocommit=False) as conn, conn.cursor() as cur:
        for pid, v in verdicts.items():
            lab = to_label(v, prompt_refs=[p_ca.ref, p_cy.ref], model=model)
            cur.execute(_UPSERT, {
                "post_id": pid, "label_version": label_version,
                "is_cyber": lab.is_cyber, "is_canada": lab.is_canada,
                "is_relevant": lab.is_relevant, "topic_key": lab.topic_key,
                "is_low_information": lab.is_low_information,
                "matched_terms": Jsonb(lab.matched_terms),
                "confidence": lab.confidence})
            llm.record(cur, post_id=pid, label_version=label_version,
                       prompts=[p_ca, p_cy], model_id=model, quant=quant,
                       params={"seed": seed, "workers": workers})
            for e in v.entities:
                cur.execute(_ENT_UPSERT, {
                    "post_id": pid, "label_version": label_version,
                    "entity_text": e["text"][:200], "entity_type": e.get("type", "other"),
                    "canonical": canonical(e["text"], e.get("type", "org")),
                    "char_start": locate(txt.get(pid, ""), e["text"])})
        conn.commit()

    stats["wall_s"] = round(time.perf_counter() - t0, 1)
    stats["posts_per_s"] = round(n / max(1e-9, stats["wall_s"]), 1)
    return stats
