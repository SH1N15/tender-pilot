"""检查报告导出执行体（G-5 从路由文件迁出；纯格式化无 LLM，路由薄壳调用）。"""

from __future__ import annotations

from core.skill_engine.base import SkillContext
from services.check.skills.check_report_export_skill import CheckReportExportSkill


async def render_check_report_export(report_data: dict, fmt: str, project_name: str) -> str:
    """把已落库的检查报告 results 渲染为 markdown/html/json 文本；失败抛 ValueError。"""
    skill = CheckReportExportSkill()
    ctx = SkillContext(
        project_id="",
        db=None,
        llm=None,
        parameters={
            "report_data": report_data,
            "format": fmt,
            "project_name": project_name,
        },
    )
    result = await skill.safe_execute(ctx)
    if not result.success:
        raise ValueError(result.error or "检查报告导出失败")
    return (result.data or {}).get("content", "")
