"""QualificationMatchSkill：招标要求—企业能力资格预审（确定性规则）。

该 Skill 在无 LLM 环境可独立运行（ctx.llm 不会被访问），
LLM 仅作为未来"解释层"预留，不构成当前执行前提。
"""

from __future__ import annotations

from core.skill_engine.base import Skill, SkillContext, SkillResult
from services.qualification.matcher import match_qualifications


class QualificationMatchSkill(Skill):
    name = "qualification_match"
    description = "招标要求—企业能力资格预审：按确定性规则逐条匹配 requirements 与 credentials，不依赖 LLM"
    category = "qualification"
    version = "1.0.0"
    triggers = ["资格预审", "资格匹配", "qualification_match"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        params = ctx.parameters or {}
        try:
            report = match_qualifications(params.get("requirements", []), params.get("credentials", []))
        except (TypeError, ValueError) as e:
            return SkillResult(success=False, error=str(e), warnings=["资格预审参数不合法"])
        return SkillResult(success=True, data=report.model_dump(), tokens_consumed=0)


__all__ = ["QualificationMatchSkill"]
