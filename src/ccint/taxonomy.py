"""分类法的唯一加载点(T1)。

在此之前 topic 列表有两份真值源:``lexicons_v2/topics.yaml`` 给规则 labeler,
``llm_v1.BUCKETS`` 给 LLM 的 guided decoding enum。两份可以各自演化,而且
**漂移了不会报错** —— 后果不是崩溃,是两个 ``label_version`` 悄悄分类到不同的
标签空间上,于是它们之间的逐行 diff 里混进了「分类法不一致」这个与模型能力
无关的成分,而整个 llm_v1 vs rules_v2 的对比正是建立在那个 diff 上的。

合并时实测到的第一个漂移:规则版 10 个桶 + 隐含 ``other``,LLM enum 11 个显式
条目。两边的标签空间大小本来就不同。

本模块只读不写,加载一次缓存。所有消费者(规则 labeler、LLM prompt、JSON
schema、评测脚本)必须经由它拿 topic 列表,不得自带字面量。
"""
from __future__ import annotations

import functools
import pathlib

import yaml

CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"


@functools.lru_cache(maxsize=1)
def load(path: pathlib.Path | None = None) -> dict:
    d = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8"))
    keys = [t["key"] for t in d["topics"]]
    if len(keys) != len(set(keys)):
        raise ValueError(f"taxonomy.yaml 有重复 key: {keys}")
    if d["residual_key"] not in keys:
        raise ValueError(f"residual_key {d['residual_key']!r} 不在 topics 里")
    if keys[-1] != d["residual_key"]:
        raise ValueError("residual 必须排在最后 —— 顺序即优先级,兜底桶不能抢先命中")
    return d


def version() -> str:
    return load()["version"]


def keys() -> list[str]:
    """全部 topic key,含 residual,顺序即优先级。用作 JSON schema 的 enum。"""
    return [t["key"] for t in load()["topics"]]


def residual() -> str:
    return load()["residual_key"]


def substantive() -> list[dict]:
    """有 terms 的实质桶(不含 residual)。规则 labeler 用这个。"""
    return [t for t in load()["topics"] if not t.get("residual")]


def descriptions() -> list[tuple[str, str]]:
    """``(key, llm_description)``,顺序即优先级。prompt 用这个。"""
    return [(t["key"], t["llm_description"]) for t in load()["topics"]]


def low_information() -> dict:
    return load()["low_information"]
