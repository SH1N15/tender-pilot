from __future__ import annotations

from core.skill_engine.base import Skill, SkillContext, SkillResult


class DocIntegrityCheckSkill(Skill):
    name = "doc_integrity"
    description = "文件完整性核查(废标预防7)"
    category = "check"
    version = "1.0.0"
    triggers = ["完整性", "文件完整性", "正副本"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        tender_text = ctx.parameters.get("tender_text", "")
        bid_text = ctx.parameters.get("bid_text", "")
        supplemental = str(ctx.parameters.get("supplemental_evidence") or "")

        messages = [
            {
                "role": "system",
                "content": """你是投标文件完整性核查专家。核查以下内容：

先判断招标文件要求的递交方式。若项目为电子投标，纸质正副本、密封袋和骑缝章不属于当前文本材料缺口，标记为“后续人工/最终导出确认”而不是失败；只有招标文件明确要求纸质递交时，才将其作为当前必检项。目录、页码和附件索引应以最终PDF装配结果为准，不能因为检查输入是章节文本就直接判定企业材料缺失。

①正本副本份数：正本和副本份数是否与招标要求一致
②密封袋标识：密封袋标识是否规范（项目名称/编号/正副本标记）
③骑缝章：骑缝章是否完整
④页码：页码是否连续完整
⑤目录：目录是否与正文对应
⑥附件清单：所有要求附件是否齐全

返回JSON:
{
  "checks": [
    {
      "check_type": "copies/sealing/counterfoil/pagination/contents/attachments",
      "check_name": "检查项名称",
      "required": "招标要求",
      "actual": "投标文件情况",
      "status": "pass/fail/warning",
      "detail": "详细说明",
      "suggestion": "修改建议"
    }
  ],
  "summary": {"total": 0, "passed": 0, "failed": 0, "warning": 0},
  "risk_level": "high/medium/low",
  "has_critical_issues": true/false
}""",
            },
            {
                "role": "user",
                "content": (
                    f"招标文件：\n{tender_text[:3000]}\n\n投标文件：\n{bid_text[:3000]}"
                    f"\n\n项目补充资料证据（附件事实来源）：\n{supplemental[:8000]}"
                ),
            },
        ]

        result = await ctx.llm.collect_json(messages=messages, temperature=0.1)
        checks = result.get("checks", []) if isinstance(result, dict) else []
        # Once the project evidence contains the required corporate proofs,
        # residual attachment/OCR/final-PDF findings are export QA warnings,
        # not proof that the enterprise lacks the material.
        if supplemental and all(token in supplemental for token in ("营业执照", "税收", "社保", "财务状况报告")):
            for item in checks:
                context = (
                    str(item.get("check_name") or "")
                    + str(item.get("detail") or "")
                    + str(item.get("suggestion") or "")
                )
                if item.get("status") == "fail" and any(
                    marker in context for marker in ("附件", "扫描件", "文本片段", "最终PDF", "目录", "有效期")
                ):
                    item["status"] = "warning"
        failed = [c for c in checks if c.get("status") == "fail"]
        result.setdefault("has_critical_issues", len(failed) > 0)
        result.setdefault("risk_level", "high" if failed else "low")

        return SkillResult(
            success=True,
            data=result,
            warnings=[f"文件完整性: {len(failed)}项不通过"] if failed else [],
        )
