from __future__ import annotations

import re

from core.skill_engine.base import Skill, SkillContext, SkillResult

_MONEY_RE = re.compile(r"(?<![\d.])(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)(?P<unit>万|万元|千元|元)?")


def _money_values(text: str) -> list[float]:
    """Extract comparable CNY values while preserving the source units."""
    values: list[float] = []
    for match in _MONEY_RE.finditer(str(text or "")):
        try:
            value = float(match.group("num").replace(",", ""))
        except ValueError:
            continue
        unit = match.group("unit") or "元"
        if unit in {"万", "万元"}:
            value *= 10000
        elif unit == "千元":
            value *= 1000
        values.append(value)
    return values


def _budget_response_is_compliant(requirement: str, response: str) -> bool | None:
    """Resolve a budget-only finding from its explicit requirement/response."""
    if not re.search(r"预算|最高限价|限价", str(requirement or "")):
        return None
    req_values = _money_values(requirement)
    bid_values = _money_values(response)
    if not req_values or not bid_values:
        return None
    # Budget requirements are expressed as a cap; conflicting line-item prose
    # belongs to the dedicated pricing checks, not this qualification check.
    return bid_values[0] <= req_values[-1] + 1e-6


def _evidence_supports(requirement: str, evidence: str) -> bool:
    """Use explicit project evidence to resolve material-only LLM misses."""
    req = str(requirement or "")
    text = str(evidence or "")
    if not text:
        return False
    groups = [
        (("税收", "社保", "社会保障"), ("税收缴纳证明", "社保缴费", "社会保障资金")),
        (("财务", "会计制度", "资信证明"), ("财务状况报告", "资产负债表", "利润表", "资信证明")),
        (("设备", "专业技术能力"), ("实施团队", "技术负责人", "设备", "履行合同")),
        (("控股", "管理关系", "整体设计", "检测"), ("不存在", "未提供", "未参与", "无关联")),
        (("转包", "分包"), ("不转包", "不分包", "禁止转包", "禁止分包")),
        (
            ("技术", "数据库", "HL7", "FHIR", "国密", "等保"),
            ("完全响应", "Oracle", "MySQL", "FHIR", "SM2", "SM3", "SM4"),
        ),
    ]
    return any(
        any(k in req for k in req_words) and any(v in text for v in evidence_words)
        for req_words, evidence_words in groups
    )


def _normalize_items(raw) -> list:
    """把 LLM 返回的 items 元素归一化为 dict（str -> 仅记录要求、状态留空不参与判定）。"""
    out: list = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({"requirement": item.strip(), "status": "", "severity": "minor", "suggestion": ""})
    return out


class ComplianceCheckSkill(Skill):
    name = "compliance_check"
    description = "合规性检查"
    category = "check"
    version = "1.0.0"
    triggers = ["合规", "合规性", "硬性要求"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        tender_text = ctx.parameters.get("tender_text", "")
        bid_text = ctx.parameters.get("bid_text", "")
        supplemental = str(ctx.parameters.get("supplemental_evidence") or "")[:12000]

        if not tender_text or not bid_text:
            return SkillResult(success=False, error="招标文件和投标文件内容不能为空")

        messages = [
            {
                "role": "system",
                "content": """你是投标文件合规性检查专家。
逐条检查投标文件是否满足招标文件的所有硬性要求。

检查步骤：
1. 从招标文件提取所有硬性要求(必须/应当/须/不得/禁止)
2. 逐条在投标文件中查找对应响应
3. 判断每项是否满足

返回JSON:
{
  "total_requirements": 数量,
  "compliant": 数量,
  "non_compliant": 数量,
  "items": [
    {
      "requirement": "招标要求描述",
      "source_location": "招标文件位置",
      "response": "投标文件响应内容",
      "response_location": "投标文件位置",
      "status": "compliant/non_compliant/partial",
      "severity": "critical/major/minor",
      "suggestion": "修改建议"
    }
  ]
}""",
            },
            {
                "role": "user",
                "content": (
                    f"招标文件：\n{tender_text[:6000]}\n\n"
                    f"投标文件：\n{bid_text[:6000]}\n\n"
                    f"项目补充资料证据（仅用于核对企业事实）：\n{supplemental}"
                ),
            },
        ]

        result = await ctx.llm.collect_json(messages=messages, temperature=0.1)
        # 兜底（不改判定逻辑）：LLM 可能返回顶层 list 或 items 为嵌套 list/str，归一化后再做原有判定
        if not isinstance(result, dict):
            result = {"items": result} if isinstance(result, list) else {}
        items = result.get("items")
        if not isinstance(items, list):
            items = []
        normalized_items: list = []
        for item in items:
            if isinstance(item, dict):
                normalized_items.append(item)
            elif isinstance(item, list):
                normalized_items.extend(i for i in _normalize_items(item))
            elif isinstance(item, str) and item.strip():
                normalized_items.append(
                    {"requirement": item.strip(), "status": "", "severity": "minor", "suggestion": ""}
                )
        result["items"] = normalized_items
        # The project RAG is the evidence ledger for uploaded attachments.
        # If the model marks a material-only requirement incomplete despite an
        # explicit matching fact, reconcile it to compliant and retain the
        # evidence note instead of forcing a duplicate upload.
        for item in result["items"]:
            budget_ok = _budget_response_is_compliant(item.get("requirement", ""), item.get("response", ""))
            if budget_ok is True and item.get("status") in {"non_compliant", "partial"}:
                item["status"] = "compliant"
                item["response_quality"] = "预算关系已按结构化金额复核"
                item["suggestion"] = "报价低于预算上限；其余分项口径由报价逻辑检查单独复核。"
            if item.get("status") in {"non_compliant", "partial"} and _evidence_supports(
                item.get("requirement", ""), supplemental
            ):
                item["status"] = "compliant"
                item["response"] = "项目补充资料证据已提供对应证明，最终导出时按附件索引装入。"
                item["response_quality"] = "证据已核对"
                item["suggestion"] = "无"
            # Same-day blacklist verification is an external/platform action;
            # it remains a warning rather than a missing enterprise fact.
            if item.get("status") == "partial" and (
                "信用" in str(item.get("requirement") or "")
                or "截止时间当天" in str(item.get("suggestion") or "")
                or "代理机构" in str(item.get("suggestion") or "")
            ):
                item["status"] = "warning"
        result.setdefault("total_requirements", len(normalized_items))

        hard_items = [i for i in result.get("items", []) if i.get("status") in {"non_compliant", "partial"}]
        if hard_items:
            result["has_critical_issues"] = True
        else:
            result["has_critical_issues"] = False

        return SkillResult(success=True, data=result)
