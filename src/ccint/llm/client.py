"""统一的 LLM 调用封装(T2)。

**为什么必须统一:** P3 要求每个数字带 provenance,但在此之前 prompt 是硬编码
在 Python 里的字符串,既不进 ``analysis_runs`` 也不进 ``post_labels``。而一个
LLM labeler 的输出 100% 由 prompt 决定 —— 改一个词结果就变,且改动不留痕迹。
于是「标签永不覆写」保住了标签,却没保住产生标签的条件:库里两行 ``llm_v1``
可能来自两个不同的 prompt,而没有任何字段能把它们区分开。

本模块让 prompt 变成带版本的文件,调用时算 sha256,连同模型名和量化格式一起
写进 ``label_provenance``。代价是每次调用多一次哈希,收益是任何一条 LLM 标签
都能反查到产生它的确切文本。

[MUST] JSON schema 里自由文本字段必须排在枚举/布尔之后。实测 vLLM 的 guided
decoding 不约束 JSON token 之间的空白,小模型写完一段长自由文本后会陷入
"\\r\\n" 死循环直到耗尽 max_tokens,对象永远收不了尾。
"""
from __future__ import annotations

import functools
import hashlib
import json
import pathlib
import time
from dataclasses import dataclass

import httpx
import yaml
from psycopg.types.json import Jsonb

PROMPT_ROOT = pathlib.Path(__file__).resolve().parents[3] / "prompts"


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    text: str
    sha256: str
    meta: dict

    @property
    def ref(self) -> str:
        return f"{self.id}/v{self.version}"


@functools.lru_cache(maxsize=32)
def load_prompt(prompt_id: str, version: int = 1) -> Prompt:
    """从 ``prompts/<id>/v<N>.md`` 读取带 frontmatter 的 prompt。

    sha256 只对**正文**算,不含 frontmatter —— changelog 的措辞变化不应该让
    所有历史标签看起来像是换了 prompt。
    """
    p = PROMPT_ROOT / prompt_id / f"v{version}.md"
    raw = p.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{p} 缺 frontmatter")
    _, fm, body = raw.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    body = body.strip()
    if meta.get("name") != prompt_id or int(meta.get("version", -1)) != version:
        raise ValueError(f"{p} 的 frontmatter 与路径不一致: {meta.get('name')!r} "
                         f"v{meta.get('version')} vs {prompt_id!r} v{version}")
    return Prompt(id=prompt_id, version=version, text=body,
                  sha256=hashlib.sha256(body.encode()).hexdigest(), meta=meta)


@dataclass
class Completion:
    data: dict | None
    prompt_sha256: str
    prompt_ref: str
    model_id: str
    latency_ms: int
    error: str | None = None


def call(http: httpx.Client, *, base_url: str, model: str, prompt: Prompt,
         user_text: str, schema: dict, max_tokens: int = 320,
         temperature: float = 0.0, seed: int = 0,
         retries: int = 3, truncate: int = 1600) -> Completion:
    """一次结构化调用。失败返回 ``data=None`` 且带 error —— 失败和判否必须可区分。"""
    body = {
        "model": model, "temperature": temperature, "max_tokens": max_tokens,
        "seed": seed,
        "messages": [{"role": "system", "content": prompt.text},
                     {"role": "user", "content": (user_text or "")[:truncate]}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "out", "schema": schema}},
    }
    t0 = time.perf_counter()
    last = "unknown"
    for attempt in range(retries):
        try:
            r = http.post(f"{base_url}/chat/completions", json=body, timeout=180)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return Completion(json.loads(content), prompt.sha256, prompt.ref,
                              model, int((time.perf_counter() - t0) * 1000))
        except Exception as e:                    # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:200]
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
    return Completion(None, prompt.sha256, prompt.ref, model,
                      int((time.perf_counter() - t0) * 1000), error=last)


_PROV = """
INSERT INTO label_provenance (post_id, label_version, prompt_id, prompt_version,
                              prompt_sha256, model_id, quant, params)
VALUES (%(post_id)s, %(label_version)s, %(prompt_id)s, %(prompt_version)s,
        %(prompt_sha256)s, %(model_id)s, %(quant)s, %(params)s)
ON CONFLICT (post_id, label_version) DO NOTHING
"""


def record(cur, *, post_id: int, label_version: str, prompts: list[Prompt],
           model_id: str, quant: str | None, params: dict) -> None:
    """写溯源。多 prompt 的 labeler 记录各自 sha 的有序拼接。"""
    cur.execute(_PROV, {
        "post_id": post_id, "label_version": label_version,
        "prompt_id": "+".join(p.id for p in prompts),
        "prompt_version": "+".join(str(p.version) for p in prompts),
        "prompt_sha256": "+".join(p.sha256[:16] for p in prompts),
        "model_id": model_id, "quant": quant, "params": Jsonb(params)})
