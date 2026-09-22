"""可视化层（v1 Phase 4）。

设计取向
--------
这些图的目的**不是**让语料看起来丰富，而是让「表面活跃 vs 真实参与」的差距
一眼可见。第一波分析测出：3% 的账号贡献 24.3% 的语料，`ransomware` 45 条里
40 条来自 5 个自动化 feed。折线图上的 raw count 会把这件事完全掩盖掉。

因此每张涉及 volume 的图都**强制三条序列**：
    raw posts / non-broadcast posts / unique authors
只画 raw 的图在本项目里视为错误。
"""
from .palette import PALETTE  # noqa: F401
