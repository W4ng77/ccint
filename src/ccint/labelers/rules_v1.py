"""规则标注器 v1。label_version = "rules_v1"。

[MUST] 词表全部在 YAML，Python 里零硬编码 —— 词表要能被非程序员审阅和修改，
且改动必须通过 bump label_version 追溯（HANDOFF §M4）。

[MUST] matched_terms 记录命中词，否则人工无法检查误判。
[MUST] is_low_information 只打 flag，不删除。
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

from ..models import Label

log = logging.getLogger(__name__)

LEXICON_ROOT = Path(__file__).resolve().parent
LEXICON_DIR = LEXICON_ROOT / "lexicons"          # rules_v1
LABEL_VERSION = "rules_v1"

# [MUST] HANDOFF §1.2：换方法 = 写入新 label_version，旧 version 保留用于并排比较。
# 词表目录按 version 分开，两套可同时加载、对同一批 posts 各写一份 post_labels。
LEXICON_DIRS = {
    "rules_v1": LEXICON_ROOT / "lexicons",
    "rules_v2": LEXICON_ROOT / "lexicons_v2",
}

_URL_RE = re.compile(r"https?://\S+", re.I)
_MENTION_RE = re.compile(r"@[\w.\-]+")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]"
)


def _load(name: str, lexicon_dir: Path | None = None) -> dict:
    d = lexicon_dir or LEXICON_DIR
    data = yaml.safe_load((d / name).read_text(encoding="utf-8"))
    _assert_all_str(data, f"{d.name}/{name}")
    return data


def _assert_all_str(data: dict, where: str) -> None:
    """带冒号的正则若忘记加引号，YAML 会静默解析成 mapping 而非字符串。

    这是真实踩过的坑：`- (?:new |another )?breach:` 被解析成 {"...breach": None}，
    到 re.compile 才炸，且错误信息（unhashable type: dict）完全指不到根因。
    """
    buckets = [t for g in (data.get("groups") or {}).values() for t in g["terms"]]
    buckets += [t for lst in (data.get("match_terms") or {}).values() for t in lst]
    buckets += [t for b in (data.get("buckets") or []) for t in b["terms"]]
    for t in buckets:
        if not isinstance(t, str):
            raise TypeError(
                f"{where}: 词条 {t!r} 不是字符串 —— 含 ':' 的正则必须加引号")


_WORDY_END = re.compile(r"[\w\)\]\?\*\+]$", re.UNICODE)
_WORDY_START = re.compile(r"^[\w\(\[\\]", re.UNICODE)


def _compile(term: str, *, case_sensitive: bool = False) -> re.Pattern:
    r"""给词条加词边界。

    \b 对 é/à 等重音字符在 Python re 的 unicode 模式下行为正确。

    [坑] \b 是**零宽断言**，只在「单词字符 ↔ 非单词字符」的交界处匹配。
    若词条以标点结尾（如 HIBP 式的 "breach:"），追加 \b 会要求其后紧跟单词字符，
    而实际文本是 "New breach: Golf" —— 冒号后是空格，断言失败，整条永不命中。
    故仅在两端确实是单词字符时才加边界。
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    anchored = term.startswith(("^", "\\b"))
    body = term if anchored else rf"(?:{term})"
    if not anchored and _WORDY_START.search(term):
        body = r"\b" + body
    if not term.endswith(("$", "\\b")) and not anchored and _WORDY_END.search(term):
        body += r"\b"
    return re.compile(body, flags | re.UNICODE)


@lru_cache(maxsize=8)
def _lexicons(lexicon_dir: Path | None = None) -> dict:
    cyber = _load("cyber.yaml", lexicon_dir)
    canada = _load("canada.yaml", lexicon_dir)
    topics = _load("topics.yaml", lexicon_dir)

    cyber_pats = [
        (lang, t, _compile(t))
        for lang, terms in (cyber.get("match_terms") or {}).items()
        for t in terms
    ]
    canada_groups = {}
    for gname, cfg in canada["groups"].items():
        cs = bool(cfg.get("case_sensitive"))
        canada_groups[gname] = {
            "weight": float(cfg.get("weight", 1.0)),
            "requires_cyber_context": bool(cfg.get("requires_cyber_context")),
            # 该组命中不足以单独认定 Canada，必须另有非 corroboration 组也命中。
            "requires_corroboration": bool(cfg.get("requires_corroboration")),
            "pats": [(t, _compile(t, case_sensitive=cs)) for t in cfg["terms"]],
        }
    guards = [
        (g["pattern"], re.compile(g["pattern"], re.IGNORECASE), g.get("reason", ""))
        for g in canada.get("negative_guards", [])
    ]
    topic_buckets = [
        (b["key"], [(t, _compile(t)) for t in b["terms"]]) for b in topics["buckets"]
    ]
    lowinfo = topics["low_information"]
    return {
        "versions": {
            "cyber": cyber["version"],
            "canada": canada["version"],
            "topics": topics["version"],
        },
        "cyber_pats": cyber_pats,
        "canada_groups": canada_groups,
        "guards": guards,
        "topic_buckets": topic_buckets,
        "lowinfo_min_tokens": int(lowinfo["min_effective_tokens"]),
        "lowinfo_pats": [re.compile(p, re.IGNORECASE) for p in lowinfo["reaction_only_patterns"]],
    }


def effective_tokens(text: str) -> list[str]:
    """去 URL、去 mention、去 emoji 后的有效 token。"""
    t = _URL_RE.sub(" ", text or "")
    t = _MENTION_RE.sub(" ", t)
    t = _EMOJI_RE.sub(" ", t)
    return [w for w in re.split(r"[\s\W_]+", t, flags=re.UNICODE) if w]


class RulesLabeler:
    """规则标注器。词表目录由 label_version 决定，多个 version 可并存。"""

    def __init__(self, label_version: str = LABEL_VERSION,
                 lexicon_dir: Path | None = None) -> None:
        self.label_version = label_version
        d = lexicon_dir or LEXICON_DIRS.get(label_version)
        if d is None:
            raise ValueError(f"未知 label_version: {label_version!r}")
        self.lexicon_dir = d
        self.lex = _lexicons(d)

    # -- 分项 --------------------------------------------------------------
    def _cyber_hits(self, text: str) -> list[str]:
        return [t for _lang, t, p in self.lex["cyber_pats"] if p.search(text)]

    def _guard_hits(self, text: str) -> list[str]:
        return [pat for pat, rx, _r in self.lex["guards"] if rx.search(text)]

    def _canada_hits(self, text: str, has_cyber: bool) -> tuple[dict[str, list[str]], float]:
        """返回 {group: [hits]} 与加权得分。guard 命中时整体作废。"""
        guards = self._guard_hits(text)
        # guard 覆盖的片段先从文本中剔除，避免 "Canada Goose" 同时喂给 geo 的 "canada"
        probe = text
        for pat, rx, _r in self.lex["guards"]:
            probe = rx.sub(" ", probe)

        hits: dict[str, list[str]] = {}
        score = 0.0
        weak: dict[str, list[str]] = {}       # 需佐证的组
        strong_found = False
        for gname, cfg in self.lex["canada_groups"].items():
            if cfg["requires_cyber_context"] and not has_cyber:
                continue
            g = [t for t, p in cfg["pats"] if p.search(probe)]
            if not g:
                continue
            if cfg["requires_corroboration"]:
                weak[gname] = g
                continue
            hits[gname] = g
            score += cfg["weight"] * len(g)
            strong_found = True

        # 歧义缩写只在另有加拿大信号时才计入。
        # [实测依据] rules_v1 的 relevant 里 15.4% 仅靠缩写认定 Canada，抽样几乎全是
        # 假阳性：CRA = EU Cyber Resilience Act，GRC = Governance/Risk/Compliance，
        # CSIS = 美国 CSIS 智库，CST = 时区。这些含义**本身就是网安术语**，
        # 故 requires_cyber_context 对它们完全无效 —— 必须改为要求加拿大侧佐证。
        for gname, g in weak.items():
            if strong_found:
                hits[gname] = g
                score += self.lex["canada_groups"][gname]["weight"] * len(g)
            else:
                hits.setdefault("_uncorroborated", []).extend(g)
        if guards:
            hits["_guards_triggered"] = guards
        return hits, score

    def _topic(self, text: str) -> tuple[str, list[str]]:
        """[MUST] 优先级有序，首个命中者胜出，全不命中归 other。"""
        for key, pats in self.lex["topic_buckets"]:
            hits = [t for t, p in pats if p.search(text)]
            if hits:
                return key, hits
        return "other", []

    def _low_information(self, text: str) -> tuple[bool, str | None]:
        stripped = (text or "").strip()
        for p in self.lex["lowinfo_pats"]:
            if p.match(stripped):
                return True, f"reaction_only:{p.pattern[:28]}"
        n = len(effective_tokens(text))
        if n < self.lex["lowinfo_min_tokens"]:
            return True, f"effective_tokens={n}"
        return False, None

    # -- Protocol ----------------------------------------------------------
    def label(self, text: str, lang: str | None = None, meta: dict | None = None) -> Label:
        text = text or ""
        cyber_hits = self._cyber_hits(text)
        is_cyber = bool(cyber_hits)
        canada_hits, ca_score = self._canada_hits(text, is_cyber)
        is_canada = any(k for k in canada_hits if not k.startswith("_"))
        topic_key, topic_hits = self._topic(text)
        low, low_reason = self._low_information(text)

        matched = {"cyber": cyber_hits, "canada": canada_hits, "topic": topic_hits}
        if low_reason:
            matched["low_information_reason"] = low_reason

        # confidence 是透明的可解释量，不是概率：命中强度的归一化。
        conf = min(1.0, (len(cyber_hits) * 0.15 + ca_score * 0.2)) if is_cyber and is_canada else None

        return Label(
            is_cyber=is_cyber,
            is_canada=is_canada,
            is_relevant=is_cyber and is_canada,   # v0 定义
            topic_key=topic_key,
            is_low_information=low,
            matched_terms=matched,
            confidence=conf,
        )

    def lexicon_versions(self) -> dict:
        return self.lex["versions"]


class RulesV1Labeler(RulesLabeler):
    """rules_v1 的具名入口（向后兼容既有测试与调用）。"""

    label_version = LABEL_VERSION

    def __init__(self) -> None:
        super().__init__("rules_v1")


class RulesV2Labeler(RulesLabeler):
    """rules_v2：修 GTA 假阳性 + 补召回。与 v1 并存，可并排计分比较。"""

    label_version = "rules_v2"

    def __init__(self) -> None:
        super().__init__("rules_v2")
