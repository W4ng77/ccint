"""Collector Protocol。[MUST] 新增 source 只写一个文件，下游不含 source-specific 逻辑。"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..models import Page


@runtime_checkable
class Collector(Protocol):
    source_key: str
    query_version: str

    def query_spec(self) -> dict:
        """序列化本次采集的完整查询条件，写入 collection_runs.query_spec。"""
        ...

    def fetch_page(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> Page:
        ...
