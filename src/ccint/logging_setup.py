"""标准 logging，结构化输出到 stdout（HANDOFF §2）。"""
from __future__ import annotations

import logging
import sys


def setup(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(h)
    root.setLevel(level)
    # pgserver 每次启停都刷一屏 INFO，降噪
    logging.getLogger("pgserver").setLevel(logging.WARNING)
