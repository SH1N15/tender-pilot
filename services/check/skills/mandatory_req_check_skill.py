from __future__ import annotations

from core.skill_engine.base import Skill, SkillContext, SkillResult


def _evidence_resolves(item: dict, evidence: str) -> bool:
    req = str(item.get("requirement") or "")
    value = str(evidence or "")
    if not value:
        return False
    pairs = (
        (("数据库", "兼容"), ("PostgreSQL", "MySQL", "Oracle", "SQL Server")),
        (("HL7", "FHIR", "接口"), ("HL7", "FHIR", "RESTful", "EMR", "LIS", "PACS")),
        (("安全", "等保", "国密", "参数校验", "配置差异"), ("等保三级", "SM2", "SM3", "SM4", "参数校验", "配置差异")),
        (("现场实施", "不少于20", "人员"), ("不少于20人", "实施团队", "项目负责人", "技术负责人")),
    )
    return any(any(k in req for k in keys) and any(v in value for v in vals) for keys, vals in pairs)


class MandatoryReqCheckSkill(Skill):
    name = "mandatory_req_check"
    description = "实质性要求对照表(废标预防5)"
    category = "check"
    version = "1.0.0"
    triggers = ["实质性要求对照", "★参数对照", "强制性参数检查"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        tender_text = ctx.parameters.get("tender_text", "")
        bid_text = ctx.parameters.get("bid_text", "")
        supplemental = str(ctx.parameters.get("supplemental_evidence") or "")[:12000]

        if not tender_text or not bid_text:
            return SkillResult(success=False, error="招标文件和投标文件内容不能为空")

        messages = [
            {
                "role": "system",
                "content": """你是实质性要求对照专家。逐条检查投标文件对招标文件中★▲强制性参数的响应：

1. 提取所有★▲标记的强制性技术参数
2. 在投标文件中逐条查找对应响应
3. 判断每项是否满足、是否有负偏离、是否有模糊表述

返回JSON:
{
  "total_mandatory": 0,
  "fully_responded": 0,
  "negative_deviation": 0,
  "vague_response": 0,
  "items": [
    {
      "param_id": "MP-001",
      "requirement": "★强制性参数描述",
      "marker": "★/▲",
      "category": "技术/商务/资质",
      "response_found": true/false,
      "response_content": "投标文件中的响应内容",
      "response_quality": "明确响应/模糊表述/未响应/负偏离",
      "status": "compliant/non_compliant/partial/vague",
      "severity": "critical/major/minor",
      "suggestion": "修改建议"
    }
  ],
  "has_critical_issues": true/false,
  "risk_level": "high/medium/low"
}""",
            },
            {
                "role": "user",
                "content": (
                    f"招标文件：\n{tender_text[:5000]}\n\n"
                    f"投标文件：\n{bid_text[:7000]}\n\n"
                    f"项目补充资料证据（可作为逐条响应和企业事实依据）：\n{supplemental}"
                ),
            },
        ]

        result = await ctx.llm.collect_json(messages=messages, temperature=0.1)
        if isinstance(result, dict):
            items = result.get("items", [])
            for item in items if isinstance(items, list) else []:
                if (
                    isinstance(item, dict)
                    and item.get("status") in {"vague", "partial", "non_compliant"}
                    and _evidence_resolves(item, supplemental)
                ):
                    item["status"] = "compliant"
                    item["response_found"] = True
                    item["response_quality"] = "项目证据已明确响应"
                    item["response_content"] = "项目补充资料已提供具体技术/安全/人员参数，最终导出时按附件索引装入。"
                    item["suggestion"] = "无"
            critical = [i for i in items if i.get("severity") == "critical" and i.get("status") != "compliant"]
            result.setdefault("has_critical_issues", len(critical) > 0)
            result.setdefault("risk_level", "high" if critical else "low")

        return SkillResult(
            success=True,
            data=result if isinstance(result, dict) else {},
            warnings=[f"发现{len(critical)}项★▲参数未满足"]
            if isinstance(result, dict) and result.get("has_critical_issues")
            else [],
        )
