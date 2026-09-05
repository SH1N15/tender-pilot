from __future__ import annotations

import logging

from core.skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)


class ContentGenSkill(Skill):
    name = "content_gen"
    description = "正文生成(四模式)，支持自动配图"
    category = "generate"
    version = "2.0.0"
    triggers = ["生成", "撰写", "写内容"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        mode = ctx.parameters.get("mode", "A")
        chapter_title = ctx.parameters.get("chapter_title", "")
        chapter_outline = ctx.parameters.get("chapter_outline", "")
        tender_context = ctx.parameters.get("tender_context", "")
        word_count_target = ctx.parameters.get("word_count", 3000)
        enable_illustration = ctx.parameters.get("enable_illustration", True)
        illustration_provider = ctx.parameters.get("illustration_provider", "default")
        illustration_size = ctx.parameters.get("illustration_size", "landscape_16_9")

        if mode == "A":
            result = await self._mode_a(ctx, chapter_title, chapter_outline, tender_context, word_count_target)
        elif mode == "B":
            result = await self._mode_b(ctx, chapter_title, chapter_outline, word_count_target)
        elif mode == "C":
            result = await self._mode_c(ctx, chapter_title, word_count_target)
        elif mode == "D":
            result = await self._mode_d(ctx, chapter_title, word_count_target)
        else:
            return SkillResult(success=False, error=f"未知模式: {mode}")

        if result.success and enable_illustration and chapter_title:
            result = await self._add_illustration(
                ctx,
                result,
                chapter_title,
                illustration_provider,
                illustration_size,
            )

        return result

    async def _add_illustration(
        self,
        ctx: SkillContext,
        content_result: SkillResult,
        chapter_title: str,
        provider: str,
        image_size: str,
    ) -> SkillResult:
        try:
            from services.generate.skills.ai_image_skill import AiImageSkill

            image_skill = AiImageSkill()
            image_ctx = SkillContext(
                project_id=ctx.project_id,
                db=ctx.db,
                llm=ctx.llm,
                parameters={
                    "prompt": f"Professional illustration for bidding document section: {chapter_title}",
                    "provider": provider,
                    "image_size": image_size,
                    "remove_watermark": True,
                    "chapter_title": chapter_title,
                    "style_hint": "professional,business,technical,clean,diagram",
                },
            )
            image_result = await image_skill.safe_execute(image_ctx)

            if image_result.success and image_result.data:
                if not content_result.data:
                    content_result.data = {}
                content_result.data["illustration"] = {
                    "image_url": image_result.data.get("image_url", ""),
                    "base64": image_result.data.get("base64", ""),
                    "provider": image_result.data.get("provider", ""),
                    "watermark_removed": image_result.data.get("watermark_removed", False),
                    "prompt": image_result.data.get("prompt", ""),
                }
            else:
                logger.info(f"Illustration generation skipped for '{chapter_title}': {image_result.error}")
                if not content_result.data:
                    content_result.data = {}
                content_result.data["illustration"] = None

        except Exception as e:
            logger.warning(f"Illustration generation failed for '{chapter_title}': {e}")
            if not content_result.data:
                content_result.data = {}
            content_result.data["illustration"] = None

        return content_result

    async def _mode_a(self, ctx, title, outline, tender_ctx, word_count):
        messages = [
            {
                "role": "system",
                "content": f"""你是标书撰写专家。请撰写"{title}"章节。
要求：
1. 内容必须针对本项目，不得使用通用模板套话
2. 字数约{word_count}字
3. 严格响应招标文件中的技术要求和评分标准
4. 在适当位置标注[插图位置]以便后续插入配图
5. 返回JSON: {{"content": "章节正文内容", "word_count": 实际字数, """
                f""""illustration_suggestions": ["建议配图1描述", "建议配图2描述"]}}""",
            },
            {
                "role": "user",
                "content": (f"章节大纲：{outline}\n\n招标要求上下文：\n{tender_ctx[:4000]}"),
            },
        ]
        result = await ctx.llm.collect_json(messages=messages, temperature=0.5)
        return SkillResult(success=True, data=result)

    async def _mode_b(self, ctx, title, outline, word_count):
        if not ctx.knowledge_base or not hasattr(ctx.knowledge_base, "retrieve"):
            return await self._mode_a(ctx, title, outline, "", word_count)
        # G-7 收尾根治：调用方可传定向检索词（章节标题+该章小节关键词），避免
        # "标题+全大纲树 JSON"稀释向量检索导致事实命中不了；未传时保持旧语义。
        retrieval_query = str(ctx.parameters.get("retrieval_query") or "").strip()
        relevant_docs = await ctx.knowledge_base.retrieve(
            query=retrieval_query or f"{title} {outline}", top_k=5
        )
        # G7-5：调用方可显式注入定向检索文档（如修复 runner 按检查缺陷定向检索的企业
        # 事实），与库内检索结果合并构建引用对照表——保证注入事实带【n】锚点、可过硬门。
        extra_docs = [d for d in (ctx.parameters.get("extra_docs") or []) if isinstance(d, dict)]
        if extra_docs:
            seen_chunks = {
                str((d.get("metadata") or {}).get("chunk_id") or d.get("text", "")[:200])
                for d in relevant_docs
            }
            for doc in extra_docs:
                key = str((doc.get("metadata") or {}).get("chunk_id") or doc.get("text", "")[:200])
                if key and key not in seen_chunks:
                    relevant_docs.append(doc)
                    seen_chunks.add(key)
        if not relevant_docs:
            # P-D2：知识不足显式标注，不静默编造
            return SkillResult(
                success=True,
                data={
                    "content": (
                        f"# {title}\n\n（知识库无据，待补充：知识库中未检索到与本章节"
                        f"相关的参考材料，需人工补充资料后重新生成。）"
                    ),
                    "word_count": 0,
                    "sources": [],
                    "citation_ledger": {},
                    "grounding": {"total": 0, "passed": 0, "rejected": 0},
                    "citation_rate": {
                        "rate": 0.0,
                        "total_citations": 0,
                        "valid_citations": 0,
                        "note": "知识库无检索结果",
                    },
                },
            )
        # P-D2 2.3：检索产物（metadata.chunk_id，P-C 交付）构建引用对照表，带编号进入生成上下文
        from core.agent_engine.evidence_gate import (
            build_ledger,
            ground_hard_facts,
            ledger_for_output,
            ledger_texts,
            make_ledger_anchor_func,
        )

        ledger = build_ledger(relevant_docs)
        materials = "\n\n".join(
            f"【引用{entry['n']}】(chunk_id={entry['chunk_id']}, source={entry['source']}):\n{entry['text'][:800]}"
            for _, entry in sorted(ledger.items())
        )
        messages = [
            {
                "role": "system",
                "content": f"""你是标书撰写专家。基于提供的编号参考材料撰写"{title}"章节。
要求：
1. 整合参考材料，改写为适合本项目的表述，不得直接复制；
2. 引用锚点规则：凡内容来自某条参考材料的，在该句末尾标注其编号标记（如【1】、【2】）；
   标记格式必须是【数字】（如【1】），不得写成【引用1】或其它变体；
   编号只能取参考材料列表中存在的编号，不得编造；
3. 硬事实纪律：金额/日期时限/资质编号/技术参数值等硬事实必须来自参考材料且有对应引用标记；
   库内无据的内容明确写"（知识库无据，待补充）"，绝不编造数值；
4. 在适当位置标注[插图位置]以便后续插入配图。
返回JSON: {{"content": "章节正文（含【n】引用标记）", "sources": [引用来源列表],
"illustration_suggestions": ["建议配图描述"]}}""",
            },
            {
                "role": "user",
                "content": f"章节大纲：{outline}\n\n编号参考材料：\n{materials[:8000]}",
            },
        ]
        result = await ctx.llm.collect_json(messages=messages, temperature=0.4)
        content = str(result.get("content", "") or "")
        # Deterministic last-mile fact binding. The model can repeat a stale
        # value or leave a scaffold even when the ledger has current facts.
        try:
            from core.agent_engine.fact_harmonizer import harmonize_content

            content, harmonization = harmonize_content(content, title, ledger)
            result["fact_harmonization"] = harmonization
        except Exception as exc:  # noqa: BLE001 - never block generation
            logger.warning("事实协调失败（保留模型原文）: %s", exc)
        # P-D2 2.1：Evidence Grounding Gate——硬事实必须命中库内原文，否则丢弃标"待补充"
        # P-G G-2：grounding_mode="defer"（图内硬门场景）跳过内联替换，返回未处理原文，
        # 由图内 grounding_hard_gate 做确定性校验+修正+降级；默认 inline 语义原样保留。
        grounding_mode = str(ctx.parameters.get("grounding_mode", "inline"))
        if grounding_mode == "defer":
            data = dict(result)
            data["content"] = content
            data["word_count"] = len(content)
            data["sources"] = [
                {"n": entry["n"], "chunk_id": entry["chunk_id"], "source": entry["source"]}
                for _, entry in sorted(ledger.items())
            ]
            data["ledger_raw"] = ledger
            data["grounding_mode"] = "defer"
            return SkillResult(success=True, data=data)
        grounding = ground_hard_facts(content, ledger)
        data = dict(result)
        data["content"] = grounding["text"]
        data["word_count"] = len(grounding["text"])
        data["sources"] = [
            {"n": entry["n"], "chunk_id": entry["chunk_id"], "source": entry["source"]}
            for _, entry in sorted(ledger.items())
        ]
        data["citation_ledger"] = ledger_for_output(ledger)
        data["grounding"] = grounding["stats"]
        data["grounding_rejected"] = grounding["rejected"]
        # P-G G-2 附加键（既有键不动，B 模式语义原样保留）：图内硬门需要
        # 全量 ledger（含 text，供确定性 suggest_anchor 反查）与放行明细。
        data["ledger_raw"] = ledger
        data["grounding_detail"] = {
            "passed": grounding["passed"],
            "rejected": grounding["rejected"],
        }
        # 引用有效率（与 eval/metrics/citation.py 同源校验）
        try:
            from eval.metrics.citation import citation_valid_rate

            data["citation_rate"] = citation_valid_rate(
                grounding["text"],
                ledger_texts(ledger),
                anchor_func=make_ledger_anchor_func(ledger),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("引用有效率计算失败: %s", e)
        return SkillResult(success=True, data=data)

    async def _mode_c(self, ctx, title, word_count):
        template = ctx.parameters.get("template_content", "")
        if not template:
            return SkillResult(success=False, error="未提供模板内容")
        messages = [
            {
                "role": "system",
                "content": f"""你是标书撰写专家。基于模板填充"{title}"章节。
保留模板结构和专业表述，替换项目特定信息。
在适当位置标注[插图位置]以便后续插入配图。
返回JSON: {{"content": "填充后的内容", "illustration_suggestions": ["建议配图描述"]}}""",
            },
            {
                "role": "user",
                "content": f"模板内容：\n{template[:6000]}\n\n项目信息：\n{ctx.parameters.get('project_info', '')}",
            },
        ]
        result = await ctx.llm.collect_json(messages=messages, temperature=0.3)
        return SkillResult(success=True, data=result)

    async def _mode_d(self, ctx, title, word_count):
        external_data = ctx.parameters.get("external_data", "")
        if not external_data:
            return SkillResult(success=False, error="未提供外部数据")
        messages = [
            {
                "role": "system",
                "content": f"""你是标书撰写专家。整合外部数据撰写"{title}"章节。
在适当位置标注[插图位置]以便后续插入配图。
返回JSON: {{"content": "整合后的内容", "illustration_suggestions": ["建议配图描述"]}}""",
            },
            {
                "role": "user",
                "content": f"外部数据：\n{external_data[:6000]}",
            },
        ]
        result = await ctx.llm.collect_json(messages=messages, temperature=0.4)
        return SkillResult(success=True, data=result)
