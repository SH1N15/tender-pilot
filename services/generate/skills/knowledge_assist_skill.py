from __future__ import annotations

import logging

from core.skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)


class KnowledgeAssistSkill(Skill):
    name = "knowledge_assist"
    description = "知识库辅助生成(检索+注入)"
    category = "generate"
    version = "1.0.0"
    triggers = ["知识库", "RAG辅助", "知识检索"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        query = ctx.parameters.get("query", "")
        kb_id = ctx.parameters.get("kb_id", "")
        top_k = ctx.parameters.get("top_k", 5)
        chapter_title = ctx.parameters.get("chapter_title", "")
        tender_context = ctx.parameters.get("tender_context", "")

        if not query and not chapter_title:
            return SkillResult(success=False, error="请提供查询内容或章节标题")

        search_query = query or chapter_title

        kb_results = []
        if kb_id:
            kb_results = await self._search_knowledge(kb_id, search_query, top_k, ctx)

        if not kb_results:
            kb_results = await self._search_all_knowledge(search_query, top_k, ctx)

        context_text = self._format_context(kb_results)

        if chapter_title and tender_context:
            enhanced_prompt = self._build_enhanced_prompt(chapter_title, tender_context, context_text)
        else:
            enhanced_prompt = context_text

        return SkillResult(
            success=True,
            data={
                "query": search_query,
                "kb_results_count": len(kb_results),
                "context": context_text,
                "enhanced_prompt": enhanced_prompt,
                "sources": [
                    {
                        "content": r.get("content", "")[:100],
                        "score": r.get("score", 0),
                        "source": r.get("metadata", {}).get("source", ""),
                    }
                    for r in kb_results
                ],
            },
        )

    async def _search_knowledge(self, kb_id: str, query: str, top_k: int, ctx: SkillContext) -> list[dict]:
        try:
            from services.database import async_session
            from services.routers.knowledge import search_knowledge_base

            async with async_session()() as db:
                result = await search_knowledge_base(kb_id, query, top_k, db)
                return result.get("results", [])
        except Exception as e:
            logger.warning(f"知识库搜索失败(kb_id={kb_id}): {e}")
            return []

    async def _search_all_knowledge(self, query: str, top_k: int, ctx: SkillContext) -> list[dict]:
        try:
            from core.rag_engine.kb_adapter import build_default_knowledge_base

            kb = await build_default_knowledge_base()
            if kb is None:
                return []
            # P-C C4 修复：原实现漏传必需参数 collection_name → TypeError 被吞、恒返回空
            return await kb.retrieve(query, top_k=top_k)
        except Exception as e:
            logger.warning(f"全局知识检索失败: {e}")
            return []

    def _format_context(self, results: list[dict]) -> str:
        if not results:
            return ""

        parts = []
        for i, r in enumerate(results, 1):
            content = r.get("content", r.get("text", ""))
            score = r.get("score", 0)
            source = r.get("metadata", {}).get("source", "未知来源")
            parts.append(f"[参考资料{i}] (来源: {source}, 相关度: {score:.2f})\n{content}")

        return "\n\n".join(parts)

    def _build_enhanced_prompt(self, chapter_title: str, tender_context: str, kb_context: str) -> str:
        prompt_parts = [
            f"章节标题: {chapter_title}",
            "",
            "招标要求上下文:",
            tender_context[:2000],
        ]

        if kb_context:
            prompt_parts.extend(
                [
                    "",
                    "知识库参考资料(请优先使用这些资料中的具体数据和案例):",
                    kb_context[:3000],
                ]
            )

        prompt_parts.extend(
            [
                "",
                "撰写要求:",
                "1. 必须针对本项目的招标要求，不得使用通用套话",
                "2. 优先引用知识库中的具体案例、数据和经验",
                "3. 确保内容与招标文件要求一一对应",
                "4. 使用专业术语，体现技术深度",
            ]
        )

        return "\n".join(prompt_parts)
