"""Labeler Protocol（HANDOFF §M4）。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Label


@runtime_checkable
class Labeler(Protocol):
    label_version: str

    def label(self, text: str, lang: str | None, meta: dict) -> Label:
        ...
