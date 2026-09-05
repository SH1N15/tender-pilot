"""G-5 T3：generate 路由的直接 Skill 执行体（从 services/routers/generate.py 迁出）。

背景：G-4 收口后大纲/章节主链路已走 generation 图（services/generate/graph_runtime.py）；
本模块承载图外的单 Skill 辅助端点执行体（结构模板/评分覆盖/强制条款抽取/单章非流式/
旧大纲直调回归路径），由 generate 路由薄壳传入 gateway/db 调用。这满足"路由文件
safe_execute 归零"；这些端点若未来入图，图节点应复用本模块函数。

测试兼容：tests monkeypatch generate_mod.get_llm_gateway 与
OutlineGenSkill.safe_execute（类级），本模块按参数接收 gateway、按类调用 Skill，
两者均不受迁移影响。
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.skill_engine.base import SkillContext
from services.models import Analysis, Chapter, Document, Outline, Project, ProjectStatus
from services.routers.route_skill_shims import (
    ContentGenSkill,
    MandatoryReqExtractSkill,
    OutlineGenSkill,
    ScoreCoverageSkill,
    StructureTemplateSkill,
)

logger = logging.getLogger(__name__)


def _iter_outline_chapters(nodes):
    """深度优先遍历大纲树的章节节点，产出 (node_id, title) 元组。"""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if node_id is None or not node.get("title"):
            continue
        yield str(node_id), str(node["title"])
        children = node.get("children", [])
        if isinstance(children, list):
            yield from _iter_outline_chapters(children)


async def do_generate_outline_direct(project_id: str, mode: str, gateway) -> dict:
    """大纲生成的直接执行逻辑（旧行为原样保留；generate 图之外的回归/测试路径）。"""
    from services.database import async_session

    session_factory = async_session()
    async with session_factory() as db:
        try:
            t0 = time.monotonic()
            logger.info(f"[大纲生成] 开始 project_id={project_id}, mode={mode}")

            result = await db.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if not project:
                return {"success": False, "error": "项目不存在"}

            doc_result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
            doc = doc_result.scalar_one_or_none()
            if not doc or not doc.parsed_content:
                return {"success": False, "error": "请先解析招标文件"}

            logger.info(
                f"[大纲生成] DB查询完成 耗时={time.monotonic() - t0:.2f}s, 文档长度={len(doc.parsed_content)}字符"
            )

            analysis_result = await db.execute(select(Analysis).where(Analysis.project_id == project.id))
            analysis = analysis_result.scalar_one_or_none()

            scoring_matrix = {}
            if analysis and analysis.scoring_matrix:
                scoring_matrix = analysis.scoring_matrix
            elif analysis and analysis.dimensions:
                scoring_dim = analysis.dimensions.get("scoring", {})
                if scoring_dim and isinstance(scoring_dim, dict):
                    scoring_items = scoring_dim.get("scoring_items", scoring_dim.get("evaluation细则", []))
                    if scoring_items and isinstance(scoring_items, list):
                        scoring_matrix = {
                            "rows": [
                                {
                                    "category": item.get("name", item.get("category", "")),
                                    "item": item.get("name", item.get("description", item.get("item", ""))),
                                    "score": item.get("score", item.get("max_score", 0)),
                                }
                                for item in scoring_items
                                if isinstance(item, dict)
                            ]
                        }

            skill = OutlineGenSkill()
            ctx = SkillContext(
                project_id=project_id,
                db=db,
                llm=gateway,
                parameters={
                    "mode": mode,
                    "document_text": doc.parsed_content,
                    "scoring_matrix": scoring_matrix,
                },
            )

            logger.info(f"[大纲生成] 调用Skill, model={getattr(gateway, 'default_model', '')}")
            skill_result = await skill.safe_execute(ctx)

            logger.info(f"[大纲生成] Skill完成, 耗时={time.monotonic() - t0:.1f}s, success={skill_result.success}")

            if skill_result.success:
                outline_data = skill_result.data.get("outline", {})
                score_mapping = outline_data.get("score_mapping", {}) if isinstance(outline_data, dict) else {}
                chapters = outline_data.get("chapters", []) if isinstance(outline_data, dict) else []

                if not chapters:
                    return {"success": False, "error": "大纲生成结果为空，请重试"}

                existing = await db.execute(select(Outline).where(Outline.project_id == project.id))
                outline = existing.scalar_one_or_none()
                if outline:
                    outline.mode = mode
                    outline.tree = outline_data
                    outline.score_mapping = score_mapping
                else:
                    outline = Outline(
                        project_id=project.id,
                        mode=mode,
                        tree=outline_data,
                        score_mapping=score_mapping,
                    )
                    db.add(outline)

                # BUG-10 修复：物化 Chapter 行（重跑大纲时先删旧行再物化，保证幂等且无孤儿行）
                await db.execute(delete(Chapter).where(Chapter.project_id == project.id))
                for node_id, node_title in _iter_outline_chapters(outline_data.get("chapters", [])):
                    db.add(
                        Chapter(
                            id=str(node_id),
                            project_id=project.id,
                            outline_id=outline.id,
                            title=node_title,
                            status="pending",
                            content="",
                        )
                    )

                project.status = ProjectStatus.OUTLINING.value
                await db.commit()

            return {
                "success": skill_result.success,
                "data": skill_result.data,
                "error": skill_result.error,
                "warnings": skill_result.warnings,
            }
        except Exception as e:
            logger.error(f"[大纲生成] 异常: {e}")
            return {"success": False, "error": str(e)}


async def run_structure_template(project_id: str, structure_type: str, db: AsyncSession, gateway) -> dict:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    doc_result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    doc = doc_result.scalar_one_or_none()
    tender_text = doc.parsed_content[:4000] if doc and doc.parsed_content else ""

    skill = StructureTemplateSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=db,
        llm=gateway,
        parameters={
            "structure_type": structure_type,
            "tender_text": tender_text,
        },
    )
    skill_result = await skill.safe_execute(ctx)

    return {
        "success": skill_result.success,
        "data": skill_result.data,
        "error": skill_result.error,
    }


async def run_score_coverage(project_id: str, db: AsyncSession, gateway) -> dict:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    analysis_result = await db.execute(select(Analysis).where(Analysis.project_id == project.id))
    analysis = analysis_result.scalar_one_or_none()
    if not analysis or not analysis.scoring_matrix:
        raise HTTPException(status_code=400, detail="请先生成评分矩阵")

    outline_result = await db.execute(select(Outline).where(Outline.project_id == project.id))
    outline = outline_result.scalar_one_or_none()
    outline_sections = []
    if outline and outline.tree:
        raw_tree = outline.tree
        if isinstance(raw_tree, dict):
            # 大纲可能以 {"chapters": [...]} 形式存储，解包取章节列表
            raw_tree = raw_tree.get("chapters") or raw_tree.get("sections") or []
        outline_sections = raw_tree if isinstance(raw_tree, list) else []

    skill = ScoreCoverageSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=db,
        llm=gateway,
        parameters={
            "scoring_matrix": analysis.scoring_matrix,
            "outline_sections": outline_sections,
        },
    )
    skill_result = await skill.safe_execute(ctx)

    return {
        "success": skill_result.success,
        "data": skill_result.data,
        "error": skill_result.error,
    }


async def run_content_chapter(
    project_id: str, chapter_id: str, mode: str, db: AsyncSession, gateway
) -> dict:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    chapter_result = await db.execute(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project.id))
    chapter = chapter_result.scalar_one_or_none()
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")

    doc_result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    doc = doc_result.scalar_one_or_none()
    tender_context = doc.parsed_content[:4000] if doc and doc.parsed_content else ""

    outline_result = await db.execute(select(Outline).where(Outline.project_id == project.id))
    outline = outline_result.scalar_one_or_none()

    # P-C C4：B 模式注入 knowledge_base（此前恒为 None，B 模式检索从未生效）
    knowledge_base = None
    if mode.upper() == "B":
        from core.rag_engine.kb_adapter import build_default_knowledge_base

        knowledge_base = await build_default_knowledge_base()
    ctx = SkillContext(
        project_id=project_id,
        db=db,
        llm=gateway,
        knowledge_base=knowledge_base,
        parameters={
            "mode": mode,
            "chapter_title": chapter.title,
            "chapter_outline": json.dumps(outline.tree, ensure_ascii=False) if outline and outline.tree else "",
            "tender_context": tender_context,
            "word_count": 3000,
        },
    )
    skill_result = await ContentGenSkill().safe_execute(ctx)

    if skill_result.success and skill_result.data:
        chapter.content = skill_result.data.get("content", "")
        chapter.mode = mode
        chapter.status = "generated"
        chapter.word_count = skill_result.data.get("word_count", len(chapter.content))
        project.status = ProjectStatus.GENERATING
        await db.flush()

    return {
        "success": skill_result.success,
        "data": skill_result.data,
        "error": skill_result.error,
        "warnings": skill_result.warnings,
    }


async def run_mandatory_extract(project_id: str, db: AsyncSession, gateway) -> dict:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return {"success": False, "error": "项目不存在"}

    doc_result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    doc = doc_result.scalar_one_or_none()
    if not doc or not doc.parsed_content:
        return {"success": False, "error": "请先解析招标文件"}

    skill = MandatoryReqExtractSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=db,
        llm=gateway,
        parameters={"document_text": doc.parsed_content},
    )
    skill_result = await skill.safe_execute(ctx)

    return {
        "success": skill_result.success,
        "data": skill_result.data,
        "error": skill_result.error,
        "warnings": skill_result.warnings,
    }
