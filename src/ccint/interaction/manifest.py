"""冻结种子清单的读取与校验。见 docs/interaction_experiment/SPEC.md §5.2。

[MUST] 邻域采集的种子只从清单文件读,不查库 —— 判定门一重跑,库里的种子集就会
悄悄变,而清单是冻结的实验输入。读取时校验 sha256 与各人群计数,任何不符都拒绝。
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..config import REPO_ROOT

MANIFEST_DIR = REPO_ROOT / "manifests" / "interaction_experiment"
ACTIVE_VERSION = "v2"

# SPEC §5.2 / §53 给定的期望值;清单若与之不符说明拿错了文件
EXPECTED = {"v2": {"in_main": 2453, "in_legacy": 2493, "in_rules": 650, "in_collect": 2631}}


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Manifest:
    version: str
    sha256: str
    rows: list[dict]

    def population(self, col: str) -> list[dict]:
        return [r for r in self.rows if r[col] == "true"]

    def counts(self) -> dict[str, int]:
        return {c: len(self.population(c)) for c in EXPECTED.get(self.version, {})}


def load(version: str = ACTIVE_VERSION) -> Manifest:
    d = MANIFEST_DIR / version
    csv_path, meta_path = d / "seed_manifest.csv", d / "seed_manifest.meta.json"
    data = csv_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    meta = json.loads(meta_path.read_text())
    if sha != meta["sha256"]:
        raise ManifestError(f"{csv_path}: sha256 {sha} != meta {meta['sha256']}")
    rows = list(csv.DictReader(data.decode().splitlines()))
    m = Manifest(version, sha, rows)
    want = EXPECTED.get(version)
    if want and m.counts() != want:
        raise ManifestError(f"{version} counts {m.counts()} != expected {want}")
    if len({r["post_uri"] for r in rows}) != len(rows):
        raise ManifestError(f"{version}: duplicate post_uri")
    return m
