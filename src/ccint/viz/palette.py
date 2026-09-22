"""设计令牌。取自 dataviz 参考调色板，值未作改动。

slot 1–3（blue / orange / aqua）是该调色板中唯一在 `--pairs all` 下
两种模式都通过全部色觉检查的三色组（CVD ΔE 9.2 light，normal-vision 24.0）。
本项目所有分类编码**只用这三个槽位**；需要第四个序列时改用小倍数（small
multiples）而不是新增颜色。

[MUST] aqua 在浅色底上的对比度低于 3:1 —— 触发 relief 规则：
所有序列必须直接标注或提供表格视图，不得只靠颜色区分。
"""
from __future__ import annotations

PALETTE = {
    # 分类（身份）
    "series": ["#2a78d6", "#eb6834", "#1baf7a"],
    "series_names": ["blue", "orange", "aqua"],
    # 顺序（量级）—— 单一色相，浅到深
    "seq": ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec",
            "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab",
            "#184f95", "#104281", "#0d366b"],
    # 表面与文字
    "surface": "#fcfcfb",
    "text": "#0b0b0b",
    "text_secondary": "#52514e",
    "muted": "#8a8880",
    "grid": "#e4e3df",
    # 状态（保留色，永不用作序列）
    "critical": "#d03b3b",
    "warning": "#fab219",
    "good": "#0ca30c",
}

# 语义别名：在本项目里这三条序列的含义是固定的，不得互换
RAW = PALETTE["series"][0]        # 全部 post（含广播号）
ORGANIC = PALETTE["series"][1]    # 剔除广播号后
AUTHORS = PALETTE["series"][2]    # 独立作者数


def rcparams() -> dict:
    """matplotlib 全局样式：细线、退隐的网格与轴、无上/右边框。"""
    return {
        "figure.facecolor": PALETTE["surface"],
        "axes.facecolor": PALETTE["surface"],
        "savefig.facecolor": PALETTE["surface"],
        "axes.edgecolor": PALETTE["grid"],
        "axes.labelcolor": PALETTE["text_secondary"],
        "axes.titlecolor": PALETTE["text"],
        "axes.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": PALETTE["grid"],
        "grid.linewidth": 0.6,
        "xtick.color": PALETTE["text_secondary"],
        "ytick.color": PALETTE["text_secondary"],
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "font.size": 9,
        "axes.titlesize": 10,
        "axes.labelsize": 9,
        "legend.frameon": False,
        "legend.fontsize": 8,
        "lines.linewidth": 2.0,
        "lines.markersize": 4,
        "figure.dpi": 150,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
    }
