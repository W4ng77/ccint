"""标注器评估底座（v1 Phase 0）。

问题：v0 交付时 **零 ground truth** —— 没人知道 rules_v1 的召回与精度。
没有这个数字，改词表就是拍脑袋：加一个词可能提升召回 5%，也可能把 77 条
GTA 游戏泄露新闻灌进语料，而两者在没有标注集时看起来一模一样。

本模块提供：
  1. 分层抽样（按 is_cyber × is_canada 四象限 + 候选词命中层），保证稀有格也被采到；
  2. 导出人工审阅用的 TSV / 导回标注；
  3. 在标注集上计算任意 label_version 的 precision / recall / F1，带 Wilson 置信区间；
  4. 两个 label_version 的并排对比（HANDOFF §1.2 的用法）。

[MUST] 标注集与 label_version 解耦：ground truth 存在独立表，任何新 version
都能在同一批人工标注上重新计分。这样换词表 = 重新计分，而不是重新标注。
"""
from __future__ import annotations

import csv
import logging
import math
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

STRATA = {
    "relevant":        "l.is_cyber AND l.is_canada",
    "cyber_only":      "l.is_cyber AND NOT l.is_canada",
    "canada_only":     "NOT l.is_cyber AND l.is_canada",
    "neither":         "NOT l.is_cyber AND NOT l.is_canada",
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 区间。小样本下比正态近似可靠得多 —— 评估集只有几百条。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - s) / d), min(1.0, (c + s) / d))


def draw_sample(label_version: str, per_stratum: int = 50, seed: str = "ccint") -> list[dict]:
    """分层抽样。

    [MUST] 必须分层：relevant 只占语料的 0.96%，简单随机抽 200 条大约只有 2 条
    正例，算不出任何有意义的 precision。分层后每格都有足够观测，再用格权重
    还原到总体。
    """
    out: list[dict] = []
    with db.connect(autocommit=True) as conn:
        for name, cond in STRATA.items():
            rows = conn.execute(
                f"""SELECT p.post_id, p.published_at, p.lang, p.text,
                           l.is_cyber, l.is_canada, l.topic_key
                    FROM posts p JOIN post_labels l ON l.post_id = p.post_id
                    WHERE l.label_version = %s AND {cond}
                    ORDER BY md5(p.post_id::text || %s) LIMIT %s""",
                (label_version, seed, per_stratum),
            ).fetchall()
            for r in rows:
                out.append({**dict(r), "stratum": name})
            log.info("stratum %-12s drew %d", name, len(rows))
    return out


def stratum_sizes(label_version: str) -> dict[str, int]:
    """每格在总体中的真实大小，用于把分层结果加权还原。"""
    sizes = {}
    with db.connect(autocommit=True) as conn:
        for name, cond in STRATA.items():
            sizes[name] = conn.execute(
                f"""SELECT count(*) AS n FROM posts p
                    JOIN post_labels l ON l.post_id = p.post_id
                    WHERE l.label_version = %s AND {cond}""",
                (label_version,),
            ).fetchone()["n"]
    return sizes


TSV_COLS = ["post_id", "stratum", "pred_cyber", "pred_canada", "pred_topic",
            "TRUE_cyber", "TRUE_canada", "TRUE_topic", "note", "published_at",
            "lang", "text"]


def export_tsv(rows: list[dict], path: Path) -> Path:
    """导出给人工审阅。TRUE_* 三列留空待填（y/n）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TSV_COLS, delimiter="\t",
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({
                "post_id": r["post_id"],
                "stratum": r["stratum"],
                "pred_cyber": "y" if r["is_cyber"] else "n",
                "pred_canada": "y" if r["is_canada"] else "n",
                "pred_topic": r.get("topic_key") or "",
                "TRUE_cyber": "", "TRUE_canada": "", "TRUE_topic": "", "note": "",
                "published_at": r["published_at"].isoformat(),
                "lang": r.get("lang") or "",
                "text": " ".join((r["text"] or "").split()),
            })
    return path


def load_tsv(path: Path) -> list[dict]:
    """读回人工标注。只保留 TRUE_cyber / TRUE_canada 都已填写的行。"""
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            tc, tk = r.get("TRUE_cyber", "").strip().lower(), r.get("TRUE_canada", "").strip().lower()
            if tc in ("y", "n") and tk in ("y", "n"):
                rows.append({
                    "post_id": int(r["post_id"]),
                    "stratum": r["stratum"],
                    "true_cyber": tc == "y",
                    "true_canada": tk == "y",
                    "true_topic": (r.get("TRUE_topic") or "").strip() or None,
                })
    return rows


def score(truth: list[dict], label_version: str) -> dict:
    """在人工标注集上给某个 label_version 计分。

    分层抽样必须按格权重还原 —— 否则 precision 会被稀有格严重高估。
    """
    if not truth:
        raise ValueError("标注集为空：先跑 `ccint eval sample` 并填写 TRUE_* 列")

    ids = [t["post_id"] for t in truth]
    with db.connect(autocommit=True) as conn:
        pred = {r["post_id"]: r for r in conn.execute(
            """SELECT post_id, is_cyber, is_canada, is_relevant, topic_key
               FROM post_labels WHERE label_version=%s AND post_id = ANY(%s)""",
            (label_version, ids)).fetchall()}
    sizes = stratum_sizes(label_version)
    drawn: dict[str, int] = {}
    for t in truth:
        drawn[t["stratum"]] = drawn.get(t["stratum"], 0) + 1

    # 每条样本代表总体中多少条
    def weight(stratum: str) -> float:
        n = drawn.get(stratum, 0)
        return (sizes.get(stratum, 0) / n) if n else 0.0

    res = {}
    for field, tkey in (("is_cyber", "true_cyber"), ("is_canada", "true_canada"),
                        ("is_relevant", None)):
        tp = fp = fn = tn = 0.0
        rtp = rfp = rfn = 0          # 未加权计数，用于置信区间
        for t in truth:
            p = pred.get(t["post_id"])
            if p is None:
                continue
            gold = (t["true_cyber"] and t["true_canada"]) if tkey is None else t[tkey]
            got = p[field]
            w = weight(t["stratum"])
            if got and gold:
                tp += w; rtp += 1
            elif got and not gold:
                fp += w; rfp += 1
            elif not got and gold:
                fn += w; rfn += 1
            else:
                tn += w
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        res[field] = {
            "precision": prec, "recall": rec, "f1": f1,
            "precision_ci": wilson(rtp, rtp + rfp),
            "recall_ci": wilson(rtp, rtp + rfn),
            "n_labeled": rtp + rfp + rfn,
            "raw": {"tp": rtp, "fp": rfp, "fn": rfn},
            "weighted": {"tp": round(tp), "fp": round(fp), "fn": round(fn)},
        }
    res["_meta"] = {"label_version": label_version, "n_truth": len(truth),
                    "strata_drawn": drawn, "strata_sizes": sizes}
    return res


def format_score(res: dict) -> str:
    m = res["_meta"]
    L = [f"# 标注器评估 — label_version={m['label_version']}",
         f"人工标注样本: {m['n_truth']} 条  分层: {m['strata_drawn']}", ""]
    L.append(f"{'字段':14s} {'precision':>22s} {'recall':>22s} {'F1':>7s} {'TP/FP/FN':>14s}")
    for field in ("is_cyber", "is_canada", "is_relevant"):
        r = res[field]
        pl, ph = r["precision_ci"]; rl, rh = r["recall_ci"]
        raw = r["raw"]
        L.append(f"{field:14s} {r['precision']:6.1%} [{pl:.0%},{ph:.0%}]"
                 f"{'':6s} {r['recall']:6.1%} [{rl:.0%},{rh:.0%}]"
                 f"{'':6s} {r['f1']:6.1%} {raw['tp']:4d}/{raw['fp']:3d}/{raw['fn']:3d}")
    L.append("")
    L.append("注：precision/recall 已按分层权重还原到总体；括号内为 Wilson 95% 区间"
             "（基于未加权计数，反映样本量带来的不确定性）。")
    return "\n".join(L)


def compare(truth: list[dict], versions: list[str]) -> str:
    """并排比较多个 label_version（HANDOFF §1.2：旧 version 保留用于并排比较）。"""
    L = ["# label_version 并排对比", ""]
    L.append(f"{'version':16s} {'cyber P/R':>18s} {'canada P/R':>18s} {'relevant P/R/F1':>24s}")
    for v in versions:
        try:
            r = score(truth, v)
        except Exception as e:  # noqa: BLE001
            L.append(f"{v:16s} (无法计分: {e})")
            continue
        c, k, rel = r["is_cyber"], r["is_canada"], r["is_relevant"]
        L.append(f"{v:16s} {c['precision']:7.1%}/{c['recall']:<7.1%}"
                 f"  {k['precision']:7.1%}/{k['recall']:<7.1%}"
                 f"  {rel['precision']:6.1%}/{rel['recall']:6.1%}/{rel['f1']:6.1%}")
    return "\n".join(L)
