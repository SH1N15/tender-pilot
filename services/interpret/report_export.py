"""解读报告导出执行体（G-5 从路由文件迁出；纯格式化无 LLM，路由薄壳调用）。"""

from __future__ import annotations

from core.skill_engine.base import SkillContext
from services.interpret.skills.interpret_export_skill import InterpretExportSkill


async def render_interpret_export(analysis_dimensions: dict, fmt: str, project_name: str) -> str:
    """把已落库的解读维度渲染为 markdown/html/json 文本；失败抛 ValueError。"""
    skill = InterpretExportSkill()
    ctx = SkillContext(
        project_id="",
        db=None,
        llm=None,
        parameters={
            "interpret_data": analysis_dimensions,
            "format": fmt,
            "project_name": project_name,
        },
    )
    result = await skill.safe_execute(ctx)
    if not result.success:
        raise ValueError(result.error or "解读报告导出失败")
    return (result.data or {}).get("content", "")
