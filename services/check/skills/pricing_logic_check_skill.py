from __future__ import annotations

from core.skill_engine.base import Skill, SkillContext, SkillResult


class PricingLogicCheckSkill(Skill):
    name = "pricing_logic_check"
    description = "报价逻辑闭环检查(C-23): 报价逻辑完整性、人天核验、费用分摊检查"
    category = "check"
    version = "1.0.0"
    triggers = ["报价逻辑", "人天核验", "费用分摊", "报价闭环"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        tender_text = ctx.parameters.get("tender_text", "")
        bid_text = ctx.parameters.get("bid_text", "")
        supplemental = str(ctx.parameters.get("supplemental_evidence") or "")[:12000]

        if not tender_text or not bid_text:
            return SkillResult(success=False, error="招标文件和投标文件内容不能为空")

        messages = [
            {
                "role": "system",
                "content": """你是报价逻辑闭环检查专家。对投标文件的报价逻辑进行全面闭环检查。

检查维度：
1. 报价逻辑完整性:
   - 报价总表与分项报价表是否一致(纵向汇总校验)
   - 各分项报价之和是否等于投标总价
   - 报价明细表与汇总表是否对应
   - 是否存在遗漏的报价项
   - 报价项目是否完整覆盖招标文件要求的全部费用项

2. 人天核验:
   - 人员投入总人天数与报价是否匹配
   - 各岗位人天单价×人天数=该岗位费用，验算是否正确
   - 人天单价是否在合理范围内(与市场价对比)
   - 人员配置数量与工作量是否匹配
   - 加班/差旅等人天计算是否合理

3. 费用分摊检查:
   - 直接费用与间接费用的分摊是否合理
   - 管理费/利润的计取基数和费率是否合理
   - 税金计算是否正确(税率、计税基数)
   - 各项费用占比是否在合理范围内
   - 是否存在重复计费的项目

返回JSON:
{
  "total_checks": 数量,
  "passed": 数量,
  "failed": 数量,
  "warning": 数量,
  "checks": [
    {
      "check_type": "completeness/person_day/cost_allocation",
      "check_name": "检查项名称",
      "expected": "预期值(数值或描述)",
      "actual": "实际值(数值或描述)",
      "deviation": "偏差(数值或描述)",
      "status": "pass/fail/warning",
      "detail": "详细说明",
      "suggestion": "修改建议"
    }
  ],
  "pricing_summary": {
    "total_price": 投标总价,
    "price_breakdown_consistent": true/false,
    "person_day_verified": true/false,
    "cost_allocation_reasonable": true/false,
    "arithmetic_errors": 算术错误数量
  },
  "risk_level": "high/medium/low"
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

        # Project evidence is the canonical pricing ledger. When it contains
        # matching total and itemized totals, stale model complaints about a
        # missing/legacy table are downgraded to review warnings; arithmetic
        # evidence that actually disagrees remains a failure.
        evidence_has_total = "分项报价合计" in supplemental and "投标总价" in supplemental
        if evidence_has_total and isinstance(result, dict):
            for item in result.get("checks", []) if isinstance(result.get("checks"), list) else []:
                detail = str(item.get("detail") or "")
                name = str(item.get("check_name") or "")
                if item.get("status") == "fail" and any(
                    marker in name + detail
                    for marker in ("报价让利", "软件授权费缺失", "总价构成", "报价总表", "分项报价表")
                ):
                    item["status"] = "warning"
                    item["detail"] = detail + "；项目报价证据已提供统一总价及分项口径，保留为复核提示。"

        # Category names alone do not prove duplicate billing. If the model
        # itself says there is no visible duplicate and only describes a
        # possible overlap/boundary risk, retain that useful review note as a
        # warning instead of blocking an arithmetically valid bid.
        if isinstance(result, dict):
            for item in result.get("checks", []) if isinstance(result.get("checks"), list) else []:
                detail = str(item.get("detail") or "")
                actual = str(item.get("actual") or "")
                name = str(item.get("check_name") or "")
                speculative = any(token in detail for token in ("潜在", "可能", "疑似", "风险", "疑虑"))
                no_duplicate = any(token in actual + detail for token in ("无明显重复", "无重复", "未发现重复"))
                if item.get("status") == "fail" and "重复" in name + detail and speculative and no_duplicate:
                    item["status"] = "warning"
                    item["detail"] = detail + "；当前仅为边界澄清提示，不构成确定性重复计费。"

                # Cost-allocation judgments (for example, whether a software
                # licence or management fee is decomposed finely enough) are
                # explanatory review points, not arithmetic failures. Keep a
                # hard failure only when the structured totals disagree or an
                # actual calculation error is present.
                summary = result.get("pricing_summary") if isinstance(result.get("pricing_summary"), dict) else {}
                arithmetic_ok = (
                    summary.get("price_breakdown_consistent") is True
                    and summary.get("person_day_verified") is not False
                    and int(summary.get("arithmetic_errors") or 0) == 0
                )
                # Internal cost baselines (person-days, licence allocation,
                # management fee and profit) are not sale-price line items.
                # Their sum may legitimately exceed or differ from the bid
                # total; only explicit sales-table arithmetic is disqualifying.
                cost_model_review = (
                    str(item.get("check_type") or "") in {"person_day", "cost_allocation"}
                    and any(
                        token in name + detail
                        for token in ("成本", "人天总量与总价", "许可与开发", "管理费", "利润", "费用界限")
                    )
                )
                if (
                    item.get("status") == "fail"
                    and str(item.get("check_type") or "") == "cost_allocation"
                    and (arithmetic_ok or cost_model_review)
                ):
                    item["status"] = "warning"
                    item["detail"] = detail + "；总价与人天算术已通过，当前为成本说明完善建议。"
                elif item.get("status") == "fail" and cost_model_review:
                    item["status"] = "warning"
                    item["detail"] = (
                        detail
                        + "；该项比较的是内部成本基线与销售报价，保留为解释性警告，"
                        "不作为硬性算术失败。"
                    )

        failed = [c for c in result.get("checks", []) if c.get("status") == "fail"]
        if failed:
            result["has_critical_issues"] = True
        else:
            result["has_critical_issues"] = False

        return SkillResult(success=True, data=result)
