"""迭代报告的图。只用 slot 1–3(CVD 全对检验通过的三色组),序列直接标注。"""
from __future__ import annotations

import json
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ccint import db
from ccint.viz.palette import PALETTE, rcparams

plt.rcParams.update(rcparams())
C = PALETTE["series"]
OUT = pathlib.Path("figures")
OUT.mkdir(exist_ok=True)


def fig_funnel():
    """三段分解。核心图:同一批事件在四个关卡上的存活数。"""
    ora = json.load(open("reports/oracle_events.json"))
    with db.connect(autocommit=True) as c:
        feeds = {r["h"] for r in c.execute("""select author_handle h from posts
            group by author_handle, author_id having count(*)>=50
            and avg(coalesce(like_count,0)+coalesce(repost_count,0))<0.5""")}
    KNOWN = {"ransomware.live", "ransomlook.bsky.social",
             "cyberintelligence.bsky.social", "ecrime.ch", "cti.fyi",
             "falconfeedsio.bsky.social", "hendryadrian.bsky.social",
             "fogolf.bsky.social", "hermes71.bsky.social"}
    isfeed = lambda h: h in feeds or h in KNOWN
    cy = lambda o: [h for h in (o["hits"] or []) if h.get("is_cyber")]
    N = len(ora)
    bars = [
        ("Listed in the registry", N, C[0]),
        ("Discussed on Bluesky", sum(1 for o in ora if cy(o)), C[0]),
        ("Collected by ccint", sum(1 for o in ora
                                   if [h for h in cy(o) if h["in_corpus"]]), C[0]),
        ("Discussed by a\nnon-feed account",
         sum(1 for o in ora if [h for h in cy(o) if not isfeed(h["handle"])]), C[1]),
    ]
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    ys = range(len(bars))[::-1]
    for y, (lab, v, col) in zip(ys, bars):
        ax.barh(y, v, color=col, height=0.62)
        ax.text(v + 0.4, y, f"{v}", va="center", ha="left", fontsize=11,
                color=PALETTE["text"], fontweight="bold")
    ax.set_yticks(list(ys))
    ax.set_yticklabels([b[0] for b in bars], fontsize=9.5)
    ax.set_xlim(0, N + 3)
    ax.set_xlabel("Canadian ransomware events, 22 Aug – 22 Sep 2026")
    ax.grid(axis="x", alpha=.5)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(OUT / "iter_funnel.pdf")
    plt.close(fig)


def fig_terms():
    """逐词产出。横轴采集量、纵轴相关帖,对数刻度看清楚代价。"""
    import re, yaml
    d = yaml.safe_load(open("src/ccint/labelers/lexicons/cyber.yaml", encoding="utf-8"))
    terms = [t for _l, lst in d["search_terms"].items() for t in lst]
    pats = {t: re.compile(re.escape(t).replace(r"\ ", r"\s+"), re.I) for t in terms}
    with db.connect(autocommit=True) as c:
        rows = c.execute("""select p.text, coalesce(bool_or(l.is_relevant)
            filter (where l.label_version='rules_v2'), false) rel
            from posts p left join post_labels l using(post_id)
            group by p.post_id, p.text""").fetchall()
    stat = {t: [0, 0] for t in terms}
    for r in rows:
        for t, pat in pats.items():
            if pat.search(r["text"] or ""):
                stat[t][0] += 1
                if r["rel"]:
                    stat[t][1] += 1
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    for t, (a, b) in stat.items():
        if a == 0:
            continue
        zero = b == 0
        ax.scatter(a, max(b, 0.5), s=34, color=C[1] if zero else C[0],
                   alpha=.85, edgecolor="none", zorder=3)
    for t in ("CVE", "cybersecurity", "ransomware", "data breach", "hacked",
              "malware", "phishing", "identity theft"):
        a, b = stat[t]
        ax.annotate(t, (a, max(b, 0.5)), textcoords="offset points",
                    xytext=(6, 5), fontsize=8.5, color=PALETTE["text_secondary"])
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("posts returned by the term (log)")
    ax.set_ylabel("of which judged relevant (log; 0 plotted at 0.5)")
    ax.grid(alpha=.5); ax.set_axisbelow(True)
    ax.text(.02, .96, "orange = the 11 terms that returned\nzero relevant posts",
            transform=ax.transAxes, va="top", fontsize=9, color=C[1])
    fig.tight_layout(); fig.savefig(OUT / "iter_terms.pdf"); plt.close(fig)


def fig_prompt():
    """实体抽取器 v1 vs v2 在三个探针集上的命中率。"""
    sets = ["Registry positives\n(should be ~100%)",
            "Posts literally saying\n\"Canada\" (should be ~100%)",
            "Registry negatives\n(should be ~0%)"]
    v1 = [55.4, 69.0, 0.5]
    v2 = [70.3, 99.3, 1.0]
    x = range(len(sets))
    w = .36
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    ax.bar([i - w/2 for i in x], v1, w, color=C[0], label="prompt v1")
    ax.bar([i + w/2 for i in x], v2, w, color=C[1], label="prompt v2")
    for i, (a, b) in enumerate(zip(v1, v2)):
        ax.text(i - w/2, a + 2, f"{a:.1f}", ha="center", fontsize=9, color=C[0])
        ax.text(i + w/2, b + 2, f"{b:.1f}", ha="center", fontsize=9, color=C[1])
    ax.set_xticks(list(x)); ax.set_xticklabels(sets, fontsize=9)
    ax.set_ylabel("posts with ≥1 Canadian entity extracted (%)")
    ax.set_ylim(0, 112); ax.grid(axis="y", alpha=.5); ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper center", ncol=2,
              bbox_to_anchor=(.5, 1.16), fontsize=9)
    fig.tight_layout(); fig.savefig(OUT / "iter_prompt.pdf"); plt.close(fig)


if __name__ == "__main__":
    fig_funnel(); fig_prompt(); fig_terms()
    print("→", [p.name for p in sorted(OUT.glob("iter_*.pdf"))])
