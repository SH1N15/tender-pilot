from __future__ import annotations

from core.skill_engine.base import Skill, SkillContext, SkillResult


class EbidSubmitCheckSkill(Skill):
    name = "ebid_submit_check"
    description = "电子投标提交核查(C-20): 电子提交格式、CA证书、文件完整性核查"
    category = "check"
    version = "1.0.0"
    triggers = ["电子投标", "电子提交", "CA证书", "电子签章"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        tender_text = ctx.parameters.get("tender_text", "")
        bid_text = ctx.parameters.get("bid_text", "")
        supplemental = str(ctx.parameters.get("supplemental_evidence") or "")[:12000]

        if not tender_text or not bid_text:
            return SkillResult(success=False, error="招标文件和投标文件内容不能为空")

        messages = [
            {
                "role": "system",
                "content": """你是电子投标提交核查专家。对电子投标文件的提交合规性进行全面核查。

区分“企业证据已提供”和“最终提交动作尚未执行”：营业执照、财务、税收、社保、CA证书信息等可由项目证据核验；最终PDF导出、电子签章覆盖、加密上传、解密测试和平台回执属于最终人工/平台操作。后者应标记为warning并说明待人工确认，不得因为文本中没有签章图像就把企业资格判为缺失或直接判定NO_BID。

核查要点：
1. 文件格式核查:
   - 投标文件格式是否符合招标文件要求(如PDF/DOCX/专用格式)
   - 文件是否可正常打开和读取
   - 文件大小是否在平台限制范围内
   - 文件命名是否符合招标文件要求

2. CA证书/电子签章核查:
   - 是否使用有效的CA数字证书进行电子签名
   - 电子签章是否覆盖所有需要签章的页面
   - 签章是否在有效期内
   - 签章人身份是否与投标主体一致
   - 是否存在未签章的关键页面

3. 文件完整性核查:
   - 投标文件各部分是否完整上传
   - 附件材料是否齐全
   - 文件是否损坏或存在乱码
   - 加密投标文件是否正确加密
   - 是否存在遗漏的必传文件

4. 提交合规性核查:
   - 是否在规定的提交截止时间前完成提交
   - 提交次数是否符合规定
   - 是否按照招标文件要求的顺序和结构组织文件

返回JSON:
{
  "total_checks": 数量,
  "passed": 数量,
  "failed": 数量,
  "warning": 数量,
  "checks": [
    {
      "check_type": "format/ca_certificate/integrity/compliance",
      "check_name": "检查项名称",
      "tender_requirement": "招标文件要求",
      "bid_content": "投标文件内容",
      "status": "pass/fail/warning",
      "detail": "详细说明",
      "suggestion": "修改建议"
    }
  ],
  "ebid_summary": {
    "format_compliant": true/false,
    "ca_valid": true/false,
    "seal_complete": true/false,
    "file_integrity": true/false,
    "missing_files": ["缺失文件列表"]
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

        # A 90-day bid-validity period is a duration, not a calendar date.
        # Some model responses incorrectly call “90日” an invalid date; keep
        # it as a warning until the final platform timestamp is confirmed.
        for item in result.get("checks", []) if isinstance(result, dict) else []:
            detail = str(item.get("detail") or "")
            if item.get("status") == "fail" and "90日" in detail and "不符合日历" in detail:
                item["status"] = "warning"
                item["detail"] = detail.replace("不符合日历规范", "表示投标有效期时长，需在最终提交时核对截止时间")

        failed = [c for c in result.get("checks", []) if c.get("status") == "fail"]
        if failed:
            result["has_critical_issues"] = True

        return SkillResult(success=True, data=result)
