"""自由问答的 agent 编排层。

与既有的 ``analyst.py`` 的分工:analyst 针对**一个已选定的 trend candidate**产出
结构化解释;assistant 接受自由提问,自行决定调用哪些工具。两者共用同一套工具
定义与同一个本地/远端后端封装,不重复实现。

[MUST] agent 只决定**调用哪个工具**,不决定**怎么算**。所有统计都在
``agent/toolset.py`` 转发到的既有模块里,是确定性的、可单独测试的。把分析搬进
prompt 会让同一个问题两次问走出不同口径。

[MUST] 回答必须受这四条既有测量结论约束(写进 system prompt,并在下方复述):
  1. 发帖量 ≠ 有机关注 —— 实测 3% 的账号产出 24.3% 的语料;
  2. 本语料不代表加拿大公众意见,只代表被监测源上观察到的活动;
  3. 置换检验已证明当前样本量上无可检出趋势,涨跌不得称为 trend;
  4. 每条断言都要能追到源 / 帖子 / 窗口 / 工具结果。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from .toolset import REGISTRY

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
MAX_TURNS = 8

SYSTEM = """You are the analysis agent for ccint, a monitor of cybersecurity
discussion collected from a configured set of sources.

Answer only from tool results. You have no knowledge of this corpus other than
what the tools return; if the tools do not support a claim, say so.

Four findings from this project constrain every answer:

1. Post volume is not public attention. A small number of automated
   threat-intelligence accounts produce a large share of the volume: 3% of
   accounts produced 24.3% of the corpus. Whenever you report activity for a
   topic, call get_actor_composition and state the feed share explicitly.
2. This corpus does not represent Canadian public opinion. Say "activity
   observed in the monitored sources", never "what Canadians are discussing".
3. Permutation testing on this corpus found no topic change distinguishable
   from noise. Describe movement as increased or decreased activity. Never call
   it a trend, and never attach significance to it unless a tool returns one.
4. Every statement must be traceable. Name the source, the window, and the
   counts you used, and cite post ids or CVE ids where the tools give them.

Vulnerability severity (NVD CVSS) and observed exploitation (CISA KEV) are
independent. Report them separately; never merge them into one risk score.

Work in a few tool calls, then answer in plain prose: a short direct answer,
then the evidence behind it. Do not invent post ids, CVE ids or counts."""


def _tool_schemas() -> list[dict]:
    """OpenAI 工具格式。参数 schema 与 toolset 的函数签名保持一一对应。"""
    d = lambda **kw: {"type": "object", "properties": kw, "additionalProperties": False}
    S = {"type": "string"}
    I = {"type": "integer"}
    L = {"type": "array", "items": {"type": "string"}}
    spec = [
        ("list_sources", "List the configured sources with their collection status.", d()),
        ("collect_sources", "Run a collection pass for the named configured sources.",
         d(sources=L, start_date=S, end_date=S)),
        ("get_latest_topics",
         "Most active topics in the last N days, with change against the preceding "
         "window. Returns neutral activity labels, not trends.",
         d(days=I, sources=L, top_k=I)),
        ("get_topic_timeseries",
         "Daily activity for a topic: all posts, non-broadcast posts, and distinct "
         "authors. Omit topic for all topics combined.",
         d(topic=S, days=I, sources=L)),
        ("inspect_topic",
         "Full profile of one topic: source breakdown, actor composition, "
         "timeseries, representative posts, entities and related CVEs.",
         d(topic=S, days=I, sources=L)),
        ("get_representative_posts",
         "Representative posts for a topic, ranked by engagement, one per author.",
         d(topic=S, days=I, limit=I)),
        ("get_actor_composition",
         "How much of a topic's volume comes from behaviourally-detected feed "
         "accounts rather than individual posters.", d(topic=S, days=I)),
        ("get_related_cves",
         "CVE identifiers linked to a topic or to one post, with NVD severity and "
         "CISA KEV status.", d(topic=S, post_id=I, days=I)),
        ("lookup_cve", "Details for one CVE and the posts mentioning it.", d(cve_id=S)),
        ("search_external_intelligence",
         "Search the external incident registry and CVE records by entity or keyword.",
         d(entity_or_topic=S)),
    ]
    return [{"type": "function",
             "function": {"name": n, "description": desc, "parameters": p}}
            for n, desc, p in spec]


@dataclass
class AgentAnswer:
    question: str
    answer: str
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    turns: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {"question": self.question, "answer": self.answer,
                "tool_calls": self.tool_calls, "model": self.model,
                "turns": self.turns, "error": self.error}


def _post(base_url: str, payload: dict, timeout: float = 240.0) -> dict:
    r = httpx.post(f"{base_url}/chat/completions", json=payload, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"model {r.status_code}: {r.text[:300]}")
    return r.json()


def _truncate(obj, limit: int = 6000) -> str:
    """工具结果进上下文前截断。完整结果仍然返回给界面,不靠模型转述。"""
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f'… [truncated, {len(s)} chars]'


def ask(question: str, *, base_url: str = DEFAULT_BASE_URL, model: str = "user",
        max_turns: int = MAX_TURNS, temperature: float = 0.0) -> AgentAnswer:
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": question}]
    calls_log: list[dict] = []
    out = AgentAnswer(question=question, answer="", model=model)

    for turn in range(max_turns):
        try:
            data = _post(base_url, {
                "model": model, "messages": messages,
                "tools": _tool_schemas(), "tool_choice": "auto",
                "temperature": temperature, "max_tokens": 1600,
            })
        except Exception as e:                                    # noqa: BLE001
            out.error = f"{type(e).__name__}: {e}"
            out.answer = ("The analysis backend is unavailable, so this question "
                          "cannot be answered from the database. No answer is "
                          "given rather than one from memory.")
            return out

        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        out.turns = turn + 1
        if not calls:
            out.answer = (msg.get("content") or "").strip()
            break

        for call in calls:
            name = call["function"]["name"]
            raw = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as e:
                result = {"error": f"arguments were not valid JSON: {e}"}
                calls_log.append({"tool": name, "args": raw[:200], "ok": False})
            else:
                fn = REGISTRY.get(name)
                if fn is None:
                    result = {"error": f"unknown tool {name!r}",
                              "available": sorted(REGISTRY)}
                    calls_log.append({"tool": name, "args": args, "ok": False})
                else:
                    t0 = time.perf_counter()
                    try:
                        result = fn(**args)
                        calls_log.append({
                            "tool": name, "args": args, "ok": True,
                            "ms": int((time.perf_counter() - t0) * 1000),
                            "result": result})
                    except Exception as e:                        # noqa: BLE001
                        result = {"error": f"{type(e).__name__}: {e}"}
                        calls_log.append({"tool": name, "args": args, "ok": False,
                                          "error": str(e)[:200]})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": _truncate(result)})
    else:
        out.answer = ("The agent did not finish investigating within the allowed "
                      "number of tool calls. Partial tool results are shown below.")

    if not out.answer and not out.error:
        out.answer = "No answer was produced."
    out.tool_calls = calls_log
    return out
