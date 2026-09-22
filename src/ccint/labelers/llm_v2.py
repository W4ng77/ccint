"""``llm_v2`` —— 把相关性判定从规则换成 LLM。

**这一层解决的问题。** 在它之前判定门是
``is_relevant = has_cyber_term AND has_canada_term AND corroborated``,
两侧都是窄工具且用 AND 连接,所以召回上限是两个词表的交集。91,551 条采集里
只有 650 条(0.72%)通过,而那 89,714 条被拒帖从未被任何语义判定看过。
一次全库筛显示被拒帖里至少 901 条含加拿大实体,其中 55.8% 的失败原因是
``is_cyber=True, is_canada=False`` —— canada 词表漏了实体,不是逻辑太严。

**两个单问,不做合取。** 实测在同一个 prompt 里问 "cyber ∧ Canada",4B 模型
会把合取坍缩成它更熟悉的那一侧:通过率 6.8%,而通过的样本绝大多数是与加拿大
无关的全球 CTI 噪声(Fortinet CVE、法国教育部、勒索组织受害者名单)。拆成两个
独立单问后加拿大侧通过率降到 1.08%,通过的样本里开始出现真正该抓到的帖。

**调用顺序是先 Canada 后 cyber,这不是优化而是测量设计。** 这个语料每一条都是
34 个安全词搜来的,所以 cyber 侧近乎全真、信息量低;加拿大侧才是稀有且卡死
召回的一侧。先跑加拿大得到全库的实体图(它同时是查询词扩展和事件链接的输入),
再只对有实体的帖判 cyber,总调用量从 2N 降到 N + εN。

**corroboration 保留但不再是 relevant 的必要条件。** 它解决的是关键词歧义,
属于精度问题;而全库筛的归因表显示它在 89,714 条被拒帖里**一条都没误杀**
(被它挡下的帖数为 0)。先把它记成独立字段量出来,再决定去留。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import Label

LABEL_VERSION = "llm_v2"

# [MUST] 布尔在前、自由文本殿后 —— 见 llm/client.py 关于 guided decoding 的说明。
CYBER_SCHEMA = {
    "type": "object",
    "properties": {
        "is_cyber": {"type": "boolean"},
        "evidence": {"type": "string", "maxLength": 80},
    },
    "required": ["is_cyber", "evidence"],
    "additionalProperties": False,
}

ENTITY_TYPES = ["org", "gov", "place", "person_public", "domain", "other"]

# entities 里 type 在前 text 在后,同一个道理:枚举先收敛,自由文本最后写。
CANADA_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ENTITY_TYPES},
                    "text": {"type": "string", "maxLength": 80},
                },
                "required": ["type", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entities"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    is_cyber: bool | None
    cyber_evidence: str | None
    entities: list[dict]
    canada_screened: bool          # 是否真的跑过 cyber 判定

    @property
    def is_canada(self) -> bool:
        return bool(self.entities)

    @property
    def is_relevant(self) -> bool:
        """None 在这里坍缩成否是安全的:cyber 阶段只在有加拿大实体时才跑,
        所以 is_cyber is None ⟹ 无实体 ⟹ is_canada 已经为否。"""
        return bool(self.is_cyber) and self.is_canada


def locate(text: str, entity_text: str) -> int:
    """实体在原文中的偏移,事后 str.find 精确算出。

    不向模型要 ``span: [start, end]`` —— 小模型算不准字符偏移,而这里是免费且
    精确的。模型只负责「这是不是一个加拿大实体」这个它擅长的判断。
    """
    if not entity_text:
        return -1
    i = text.find(entity_text)
    return i if i >= 0 else text.lower().find(entity_text.lower())


def to_label(v: Verdict, *, prompt_refs: list[str], model: str) -> Label:
    """[MUST] topic_key 留 NULL —— llm_v2 只管相关性,主题是 llm_v3 的事。

    把两件事塞进一个 label_version 会让后续的 diff 无法归因:主题分布变了,
    是因为判定门放宽带进了新帖,还是因为主题分类器变了?
    """
    return Label(
        # [MUST] 三态:None = 从未判定(该帖没有加拿大实体,cyber 阶段根本没跑)。
        # 存成 False 会把「没问过」和「问过且答否」混成一个值 —— 与 actor 层
        # ``unclassified`` 不得当作阴性是同一条规矩。
        is_cyber=v.is_cyber,
        is_canada=v.is_canada,
        is_relevant=v.is_relevant,
        topic_key=None,
        is_low_information=False,
        matched_terms={
            "gate": "llm_v2",
            "model": model,
            "prompts": prompt_refs,
            "cyber_evidence": v.cyber_evidence,
            "canada_entities": v.entities,
            "cyber_judged": v.canada_screened,
        },
        confidence=None,
    )
