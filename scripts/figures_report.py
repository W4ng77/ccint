"""新版报告用图。沿用 slot 1–3(CVD 全对检验通过的三色组),序列直接标注。"""
from __future__ import annotations

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

KEV_C, OTHER_C = C[1], C[0]          # orange = 已被实际利用, blue = 其余


def fig_cve_attention():
    """注意力 vs 实际被利用。左:占比对照;右:被讨论最多的标识符。

    两个 panel 是小倍数,不是双轴 —— 左边是比例、右边是计数,合成一张图会骗人。
    KEV 身份除颜色外还直接标了 "KEV",不靠颜色单独承载身份。
    """
    with db.connect(autocommit=True) as c:
        agg = c.execute("""
            SELECT count(DISTINCT pc.cve_id)                                  AS ids,
                   count(DISTINCT pc.cve_id) FILTER (WHERE r.known_exploited) AS ids_kev,
                   count(*)                                                   AS men,
                   count(*) FILTER (WHERE r.known_exploited)                  AS men_kev
              FROM post_cves pc LEFT JOIN cve_records r ON r.cve_id = pc.cve_id
        """).fetchone()
        top = c.execute("""
            SELECT pc.cve_id,
                   count(*) AS n,
                   bool_or(coalesce(r.known_exploited, false)) AS kev
              FROM post_cves pc LEFT JOIN cve_records r ON r.cve_id = pc.cve_id
             GROUP BY pc.cve_id ORDER BY n DESC LIMIT 20
        """).fetchall()

    fig, (axl, axr) = plt.subplots(
        1, 2, figsize=(7.4, 4.0), gridspec_kw={"width_ratios": [1, 1.5]})

    # --- 左:两个总体的 KEV 占比 -------------------------------------------
    rows = [("Distinct identifiers\nmentioned  (n = %s)" % f'{agg["ids"]:,}',
             agg["ids_kev"] / agg["ids"], agg["ids"]),
            ("Mentions in the\ncorpus  (n = %s)" % f'{agg["men"]:,}',
             agg["men_kev"] / agg["men"], agg["men"])]
    for i, (lab, frac, tot) in enumerate(rows):
        axl.barh(i, frac * 100, color=KEV_C, height=0.5)
        axl.barh(i, 100 - frac * 100, left=frac * 100 + 0.6,
                 color=OTHER_C, alpha=.22, height=0.5)
        axl.text(frac * 100 + 2.5, i, f"{frac*100:.1f}%", va="center",
                 fontsize=11, fontweight="bold", color=PALETTE["text"])
    axl.set_yticks([0, 1]); axl.set_yticklabels([r[0] for r in rows], fontsize=8.5)
    axl.set_xlim(0, 100); axl.set_xticks([0, 50, 100])
    axl.set_xticklabels(["0", "50", "100%"])
    axl.set_xlabel("share carried by KEV-listed identifiers")
    axl.grid(axis="x", alpha=.5); axl.set_axisbelow(True)
    axl.set_title("A  Concentration", loc="left", fontweight="bold")

    # --- 右:被讨论最多的标识符 --------------------------------------------
    ys = list(range(len(top)))[::-1]
    for y, r in zip(ys, top):
        axr.barh(y, r["n"], color=KEV_C if r["kev"] else OTHER_C, height=0.62)
        axr.text(r["n"] + 4, y, f'{r["n"]}' + ("  KEV" if r["kev"] else ""),
                 va="center", fontsize=8,
                 color=PALETTE["text"] if r["kev"] else PALETTE["text_secondary"],
                 fontweight="bold" if r["kev"] else "normal")
    axr.set_yticks(ys); axr.set_yticklabels([r["cve_id"] for r in top], fontsize=7.0)
    axr.set_xlim(0, max(r["n"] for r in top) * 1.3)
    axr.set_xlabel("posts mentioning the identifier")
    axr.grid(axis="x", alpha=.5); axr.set_axisbelow(True)
    axr.set_title("B  Most-discussed identifiers", loc="left", fontweight="bold")

    h = [plt.Rectangle((0, 0), 1, 1, color=KEV_C),
         plt.Rectangle((0, 0), 1, 1, color=OTHER_C)]
    fig.legend(h, ["in the CISA KEV catalogue", "not listed as exploited"],
               loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.045))
    fig.tight_layout()
    fig.savefig(OUT / "fig_cve_attention.pdf", bbox_inches="tight")
    plt.close(fig)
    print("cve attention:", dict(agg))


def fig_recent_topics():
    """近 14 天与上一个 14 天的对照。两条序列都直接标注,并写明这不是趋势。"""
    import sys
    sys.path.insert(0, "src")
    from ccint.agent import toolset as ts
    t = ts.get_latest_topics(days=14)["topics"]
    t = [r for r in t if r["topic"] != "(unlabelled)"][:9][::-1]
    ys = list(range(len(t)))
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    for y, r in zip(ys, t):
        ax.barh(y + 0.19, r["posts"], color=C[0], height=0.34)
        ax.barh(y - 0.19, r["prev_posts"], color=C[0], alpha=.28, height=0.34)
        ax.text(r["posts"] + 4, y + 0.19, f'{r["posts"]}', va="center",
                fontsize=8, color=PALETTE["text"], fontweight="bold")
        ax.text(r["prev_posts"] + 4, y - 0.19, f'{r["prev_posts"]}', va="center",
                fontsize=8, color=PALETTE["text_secondary"])
    ax.set_yticks(ys)
    ax.set_yticklabels([r["topic"].replace("_", " ") for r in t], fontsize=8.5)
    ax.set_xlim(0, max(r["posts"] for r in t) * 1.13)
    ax.set_xlabel("relevant posts")
    ax.grid(axis="x", alpha=.5); ax.set_axisbelow(True)
    h = [plt.Rectangle((0, 0), 1, 1, color=C[0]),
         plt.Rectangle((0, 0), 1, 1, color=C[0], alpha=.28)]
    ax.legend(h, ["last 14 days", "preceding 14 days"], loc="lower right")
    fig.tight_layout()
    fig.savefig(OUT / "fig_recent_topics.pdf")
    plt.close(fig)
    print("recent topics:", [(r["topic"], r["posts"], r["prev_posts"]) for r in t])


if __name__ == "__main__":
    fig_cve_attention()
    fig_recent_topics()
