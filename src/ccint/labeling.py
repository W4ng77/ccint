"""标注写库：对 posts 跑 Labeler，结果写入 post_labels。

[MUST] 相关性判定在下游且带版本（HANDOFF §1.2）。换方法 = 写入新 label_version，
posts 表一个字节都不动，旧 version 保留用于并排比较。
"""
from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

from . import db

log = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO post_labels
  (post_id, label_version, is_cyber, is_canada, is_relevant,
   topic_key, is_low_information, matched_terms, confidence)
VALUES (%(post_id)s, %(label_version)s, %(is_cyber)s, %(is_canada)s, %(is_relevant)s,
        %(topic_key)s, %(is_low_information)s, %(matched_terms)s, %(confidence)s)
ON CONFLICT (post_id, label_version) DO UPDATE SET
  is_cyber=EXCLUDED.is_cyber, is_canada=EXCLUDED.is_canada,
  is_relevant=EXCLUDED.is_relevant, topic_key=EXCLUDED.topic_key,
  is_low_information=EXCLUDED.is_low_information,
  matched_terms=EXCLUDED.matched_terms, confidence=EXCLUDED.confidence,
  labeled_at=now()
"""

_SELECT_ALL = "SELECT post_id, text, lang FROM posts ORDER BY post_id"
_SELECT_UNLABELED = """
SELECT p.post_id, p.text, p.lang FROM posts p
LEFT JOIN post_labels l ON l.post_id = p.post_id AND l.label_version = %s
WHERE l.post_id IS NULL
ORDER BY p.post_id
"""


def get_labeler(version: str):
    from .labelers.rules_v1 import LEXICON_DIRS, RulesLabeler

    if version in LEXICON_DIRS:
        return RulesLabeler(version)
    raise ValueError(
        f"unknown label_version: {version!r}（已知：{sorted(LEXICON_DIRS)}）")


def label_posts(version: str = "rules_v1", *, only_unlabeled: bool = True,
                batch_size: int = 2000) -> dict:
    """对 posts 跑标注。幂等：同样输入产生同样的 post_labels 行。"""
    lab = get_labeler(version)
    stats = {"n_seen": 0, "n_written": 0, "n_relevant": 0, "n_low_info": 0}

    # 读写分离：server-side cursor 会在 commit 时失效，故读连接独立且全程不提交。
    with db.connect(autocommit=False) as rconn, db.connect(autocommit=False) as wconn:
        with rconn.cursor(name="label_cur") as cur:
            cur.itersize = batch_size
            if only_unlabeled:
                cur.execute(_SELECT_UNLABELED, (version,))
            else:
                cur.execute(_SELECT_ALL)

            buf: list[dict] = []

            def _flush() -> None:
                if not buf:
                    return
                with wconn.cursor() as wcur:
                    wcur.executemany(_UPSERT, buf)
                wconn.commit()
                stats["n_written"] += len(buf)
                buf.clear()

            for row in cur:
                r = lab.label(row["text"], row["lang"], {"post_id": row["post_id"]})
                stats["n_seen"] += 1
                stats["n_relevant"] += int(r.is_relevant)
                stats["n_low_info"] += int(r.is_low_information)
                buf.append({
                    "post_id": row["post_id"],
                    "label_version": version,
                    "is_cyber": r.is_cyber,
                    "is_canada": r.is_canada,
                    "is_relevant": r.is_relevant,
                    "topic_key": r.topic_key,
                    "is_low_information": r.is_low_information,
                    "matched_terms": Jsonb(r.matched_terms),
                    "confidence": r.confidence,
                })
                if len(buf) >= batch_size:
                    _flush()
                    log.info("labeled %d posts...", stats["n_seen"])
            _flush()

    log.info("label %s done: %s", version, stats)
    return stats


def label_counts(version: str) -> dict:
    with db.connect(autocommit=True) as conn:
        return dict(conn.execute(
            """SELECT count(*) AS n_labeled,
                      count(*) FILTER (WHERE is_relevant) AS n_relevant,
                      count(*) FILTER (WHERE is_low_information) AS n_low_info
               FROM post_labels WHERE label_version=%s""",
            (version,),
        ).fetchone())
