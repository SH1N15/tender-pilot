"""招投标实体抽取双路（P-A 交付件 2.4）：规则路（锚）+ LLM 路（补盲区）+ 冲突对账。

- 规则路：金额/日期/资质名称/证件编号/比例/保证金/工期 正则+词表（词表为模块常量，可维护）；
- LLM 路：结构化 JSON schema（gateway collect_json，temperature=0），需引用原文片段做证据校验；
- 对账：规则结果为锚（高置信）；LLM 结果仅在与规则不冲突或规则未覆盖时采纳，
  同类型近上下文但值不同 → 冲突标记 review_status="待审"；
- 实体带 page + evidence（命中原文片段）位置证据。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 词表与正则（规则路）
# ---------------------------------------------------------------------------

QUALIFICATION_LEXICON = [
    "营业执照",
    "医疗器械生产许可证",
    "医疗器械经营许可证",
    "医疗器械注册证",
    "ISO9001",
    "ISO14001",
    "ISO45001",
    "建筑装修装饰工程专业承包资质",
    "建筑机电安装工程专业承包资质",
    "电子与智能化工程专业承包资质",
    "建筑工程施工总承包资质",
    "安全生产许可证",
    "电信设备进网许可证",
    "放射诊疗许可证",
    "大型医用设备配置许可证",
    "特种设备生产许可证",
    "计量器具型式批准证书",
    "软件企业证书",
    "高新技术企业证书",
]

# 实体类型 -> 正则（命名组 value）
RULE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("amount", re.compile(r"(?P<value>[一二三四五六七八九十百\d][\d,，.]*\s*(?:万元|元|亿))")),
    ("date", re.compile(r"(?P<value>\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)")),
    ("date", re.compile(r"(?P<value>\d{4}\s*年\s*\d{1,2}\s*月(?!\s*\d{1,2}\s*日))")),
    ("ratio", re.compile(r"(?P<value>\d+(?:\.\d+)?\s*%)")),
    ("deposit", re.compile(r"投标保证金[^。；\n]{0,30}?(?P<value>\d[\d,，.]*\s*(?:万元|元))")),
    ("duration", re.compile(r"(?P<value>\d+\s*(?:日历天|工作日))")),
    ("duration", re.compile(r"(?:工期|质保期|服务期|维保期|保修期)[^。；\n]{0,10}?(\d+\s*个?月)")),
    ("certificate_no", re.compile(r"统一社会信用代码[:：\s]*([0-9A-HJ-NPQRTUWXY]{18})")),
    ("certificate_no", re.compile(r"(?:注册证|许可证)(?:编号|号)[:：\s]*([0-9A-Za-z\-_/]{6,30})")),
]

NORMALIZE_RULES = {
    "amount": lambda v: re.sub(r"[\s,，]", "", v),
    "date": lambda v: re.sub(r"\s", "", v),
    "ratio": lambda v: re.sub(r"\s", "", v),
    "deposit": lambda v: re.sub(r"[\s,，]", "", v),
    "duration": lambda v: re.sub(r"\s", "", v),
    "certificate_no": lambda v: re.sub(r"\s", "", v),
    "qualification": lambda v: v.strip(),
}

VALID_TYPES = set(NORMALIZE_RULES)


@dataclass
class Entity:
    entity_type: str
    value: str  # 原文值
    norm: str  # 归一化值
    source: str  # rule|llm
    confidence: float
    page: int = 0
    evidence: str = ""  # 命中原文片段（≤120 字）
    conflict: bool = False
    review_status: str = "auto"  # auto|待审

    def to_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "value": self.value,
            "norm": self.norm,
            "source": self.source,
            "confidence": self.confidence,
            "page": self.page,
            "evidence": self.evidence[:120],
            "conflict": self.conflict,
            "review_status": self.review_status,
        }


# ---------------------------------------------------------------------------
# 规则路
# ---------------------------------------------------------------------------


def rule_extract(text: str, page: int = 0) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[tuple[str, str, int]] = set()

    def add(etype: str, value: str, evidence: str, confidence: float):
        norm = NORMALIZE_RULES.get(etype, lambda v: v)(value)
        key = (etype, norm, page)
        if key in seen or not norm:
            return
        seen.add(key)
        entities.append(
            Entity(
                entity_type=etype,
                value=value.strip(),
                norm=norm,
                source="rule",
                confidence=confidence,
                page=page,
                evidence=evidence,
            )
        )

    for etype, pattern in RULE_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group("value") if "value" in pattern.groupindex else m.group(1)
            start = max(0, m.start() - 20)
            evidence = text[start : m.end() + 20].replace("\n", " ")
            add(etype, value, evidence, 0.95)

    for name in QUALIFICATION_LEXICON:
        for m in re.finditer(re.escape(name), text):
            start = max(0, m.start() - 20)
            evidence = text[start : m.end() + 20].replace("\n", " ")
            add("qualification", name, evidence, 0.9)
    return entities


# ---------------------------------------------------------------------------
# LLM 路（补盲区）
# ---------------------------------------------------------------------------

LLM_SCHEMA_PROMPT = (
    "从以下招标文件片段中抽取招投标实体。类型限定：amount(金额)/date(日期)/qualification(资质名称)/"
    "certificate_no(证件编号)/ratio(比例)/deposit(保证金)/duration(工期)。"
    "只抽取片段中明确出现的内容，quote 必须为片段原文连续子串。"
    '返回JSON: {"entities":[{"type":"amount","value":"100万元","quote":"预算金额100万元"}]}'
)


def _llm_extract_batch(texts: list[str], llm_gateway) -> list[list[dict]]:
    import asyncio

    async def _run() -> list[list[dict]]:
        results: list[list[dict]] = []
        for text in texts:
            messages = [
                {"role": "system", "content": LLM_SCHEMA_PROMPT},
                {"role": "user", "content": text[:6000]},
            ]
            try:
                data = await llm_gateway.collect_json(messages, temperature=0.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 实体抽取失败（跳过该批）: %s", exc)
                results.append([])
                continue
            if isinstance(data, list):  # 模型可能直接返回实体数组
                items = data
            elif isinstance(data, dict):
                items = data.get("entities", [])
            else:
                items = []
            out = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                etype = str(item.get("type", "")).strip()
                value = str(item.get("value", "")).strip()
                quote = str(item.get("quote", "")).strip()
                if etype in VALID_TYPES and value and quote and quote in text:
                    out.append({"type": etype, "value": value, "quote": quote})
            results.append(out)
        return results

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 对账合并
# ---------------------------------------------------------------------------


def _norm_conflict(a: Entity, b_norm: str) -> bool:
    """同类型且归一化值不同视为冲突（金额单位差异已归一）。"""
    return a.norm != b_norm


def merge_entities(rule_ents: list[Entity], llm_ents: list[Entity]) -> list[Entity]:
    """规则为锚；LLM 同类型+近值去重采纳，冲突标记待审。"""
    merged: list[Entity] = list(rule_ents)
    for le in llm_ents:
        same_type = [r for r in rule_ents if r.entity_type == le.entity_type]
        near = [r for r in same_type if r.norm == le.norm]
        if near:
            continue  # 规则已覆盖
        conflict_hit = None
        for r in same_type:
            # 同页同类型但值不同 → 冲突（页码即上下文代理；evidence 供人工复核）
            if r.page == le.page:
                conflict_hit = r
                break
        if conflict_hit is not None:
            le.conflict = True
            le.review_status = "待审"
            conflict_hit.conflict = True
            conflict_hit.review_status = "待审"
            merged.append(le)
        else:
            le.review_status = "auto"
            merged.append(le)
    return merged


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def extract_entities(text: str, pages: list[str] | None = None, llm_gateway=None, use_llm: bool = True) -> dict:
    """双路抽取。

    text: 全文（规则路主输入）；pages: 按页文本（可选，用于精确页码）；
    llm_gateway: LLMGateway 实例（可选）；use_llm: 是否启用 LLM 路。
    返回 {entities: [...], stats: {...}}。
    """
    page_texts = pages if pages else [text]
    rule_ents: list[Entity] = []
    for page_no, ptext in enumerate(page_texts, start=1):
        rule_ents.extend(rule_extract(ptext, page=page_no if pages else 0))

    llm_ents: list[Entity] = []
    if use_llm and llm_gateway is not None:
        # 分批（按页聚合到 ~4000 字）供 LLM 补盲区
        batches: list[tuple[str, int]] = []
        cur, cur_page = "", 1
        for page_no, ptext in enumerate(page_texts, start=1):
            if len(cur) + len(ptext) > 4000 and cur:
                batches.append((cur, cur_page))
                cur = ""
            if not cur:
                cur_page = page_no
            cur += ptext + "\n"
        if cur.strip():
            batches.append((cur, cur_page))
        for btext, bpage in batches:
            for item in _llm_extract_batch([btext], llm_gateway)[0]:
                quote = item["quote"]
                if quote in btext:
                    page = bpage
                else:
                    page = next((i + 1 for i, p in enumerate(page_texts) if quote in p), bpage)
                llm_ents.append(
                    Entity(
                        entity_type=item["type"],
                        value=item["value"],
                        norm=NORMALIZE_RULES.get(item["type"], lambda v: v)(item["value"]),
                        source="llm",
                        confidence=0.7,
                        page=page if pages else 0,
                        evidence=quote,
                    )
                )

    merged = merge_entities(rule_ents, llm_ents)
    stats = {
        "rule_entities": len(rule_ents),
        "llm_entities": len(llm_ents),
        "total": len(merged),
        "conflicts": sum(1 for e in merged if e.conflict),
        "by_type": {t: sum(1 for e in merged if e.entity_type == t) for t in sorted(VALID_TYPES)},
    }
    return {"entities": [e.to_dict() for e in merged], "stats": stats}
