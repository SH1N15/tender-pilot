"""G-6 T1: 生产修复 runner——检查队列 → 单章 B 模式重写 → Grounding 硬门 → 落库 → 相关项复检。

复用既有被测管线，不新建引擎：
- 章节重写：ContentGenSkill（B 模式检索注入），修复指令注入 chapter_outline；
- Grounding 硬门：core.agent_engine.generate_graph.run_gate_node（不豁免，ledger 更新）；
- 落库：run_persist_node（Chapter 表 + citation_ledger）；
- 复检：graph_adapter.run_all_checks 按 check_id 单项重跑。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_REPAIR_DIRECTIVE = (
    "\n【本次为检查驱动重写，必须修复以下问题】\n"
    "- 检查发现：{finding}\n"
    "- 条款依据：{tender_basis}\n"
    "- 修复建议：{suggestion}\n"
    "重写时在覆盖大纲全部小节的前提下，针对性落实上述修复建议，其余内容保持与原稿语义一致。"
)

# G7-5：定向检索到的企业事实随指令显式下发，要求正文消费（引用编号/金额/日期）。
# G-7 收尾：指令文本抽到公共模块 core.agent_engine.fact_directive（生成路径共用），此处保留别名。
from core.agent_engine.fact_directive import (  # noqa: E402
    FACT_DIRECTIVE as _FACT_DIRECTIVE,
)
from core.agent_engine.fact_directive import (  # noqa: E402
    PLACEHOLDER_DIRECTIVE as _PLACEHOLDER_DIRECTIVE,
)

# G7-5：事实型（物理行为/线下要件）缺陷——文本重写无法根治，复检时单独统计口径。
_FACTUAL_FINDING_RE = re.compile(r"加盖?(物理|实体)?公章|盖章|签章|手写|手签|原件|现场|密封章|骑缝章")

# G7-R3：招标摘录抽取的金额/工期/质保等跨表一致性要件关键词域。
_TENDER_FACT_KEYWORDS = ("金额", "预算", "报价", "工期", "日历日", "质保", "有效期", "投标", "名称")


def _extract_tender_facts(tender_text: str, task: dict, max_docs: int = 4) -> list[dict]:
    """G7-R3：从招标文本确定性抽取与缺陷相关、含数字的句子（预算/工期/质保等），
    作为 extra_docs 进入引用台账——修复稿因此可填占位符且过硬门（值来自台账）。"""
    text = str(tender_text or "")
    if not text:
        return []
    probe = str(task.get("finding", "")) + str(task.get("suggestion", "")) + str(task.get("tender_basis", ""))
    keywords = [k for k in _TENDER_FACT_KEYWORDS if k in probe] or _TENDER_FACT_KEYWORDS[:4]
    scored: list[tuple[int, str]] = []
    for seg in re.split(r"[。\n]", text):
        s = seg.strip()
        if not (15 <= len(s) <= 400) or not re.search(r"\d", s):
            continue
        hits = sum(1 for k in keywords if k in s)
        # G7-R3：工期句常见"180 日内/90 个日历日内"写法不含关键词，补模式加分
        if re.search(r"\d+\s*(个)?日(历日)?内", s):
            hits += 1
        if hits:
            scored.append((hits, s))
    scored.sort(key=lambda x: (-x[0], -len(x[1])))
    return [
        {"text": s[:400], "score": 1.0, "metadata": {"chunk_id": f"tender_fact_{i}", "source": "招标文件摘录"}}
        for i, (_, s) in enumerate(scored[:max_docs], start=1)
    ]


async def _retrieve_repair_facts(
    knowledge_base: Any, task: dict, chapter_title: str, top_k: int = 6
) -> tuple[list[dict], str]:
    """G7-5 根因修复：按检查缺陷定向检索（finding+suggestion+章节标题），企业域（kb_ent_*）
    命中优先。返回 (注入用 docs, 人类可读事实文本)；检索失败降级为空，不阻断修复。"""
    query = " ".join(
        str(part)
        for part in (task.get("finding"), task.get("suggestion"), task.get("tender_basis"), chapter_title)
        if part
    ).strip()
    if knowledge_base is None or not hasattr(knowledge_base, "retrieve") or not query:
        return [], ""
    try:
        hits = await knowledge_base.retrieve(query=query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning("修复定向检索失败（降级为无事实注入）: %s", exc)
        return [], ""
    def _is_ent(doc: dict) -> bool:
        return str((doc.get("metadata") or {}).get("collection", "")).startswith("kb_ent")
    ent = [h for h in hits if _is_ent(h)]
    if not ent:
        # G7-R3：缺陷定向词检索不到企业域时，用企业画像兜底查询再取一次
        #（企业信息一致性缺陷要求公司全称/信用代码等必须可填）。
        try:
            hits2 = await knowledge_base.retrieve(
                query=f"投标人 公司全称 统一社会信用代码 法定代表人 资质证书 {chapter_title}",
                top_k=top_k,
            )
            ent = [h for h in hits2 if _is_ent(h)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("企业画像兜底检索失败: %s", exc)
    picked = (ent + [h for h in hits if not _is_ent(h)])[:top_k]
    facts_text = "\n\n".join(
        f"[fact{i}] collection={(d.get('metadata') or {}).get('collection', '')} "
        f"source={(d.get('metadata') or {}).get('source', '')}:\n{str(d.get('text', ''))[:600]}"
        for i, d in enumerate(picked, start=1)
    )
    return picked, facts_text


async def _load_chapter_context(project_id: str, chapter_id: str) -> dict | None:
    """只读取章节/大纲/招标上下文（与 generate 图 run_chapter_node 同口径）。"""
    from sqlalchemy import select

    from services.database import async_session
    from services.models import Analysis, Document, Outline, Project
    from services.models import Chapter as ChapterRow

    async with async_session()() as db:
        row = (
            await db.execute(
                select(ChapterRow).where(ChapterRow.id == chapter_id, ChapterRow.project_id == project_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
        outline = (await db.execute(select(Outline).where(Outline.project_id == project_id))).scalar_one_or_none()
        # Worker J：解读 dimensions（采购人/项目名等已确认要素）与生成图同口径读取
        analysis = (
            await db.execute(select(Analysis).where(Analysis.project_id == project_id))
        ).scalar_one_or_none()
        analysis_dimensions = analysis.dimensions if analysis and isinstance(analysis.dimensions, dict) else {}
        tender_context = ""
        doc = None
        if project and project.tender_doc_id:
            doc = (
                await db.execute(select(Document).where(Document.id == project.tender_doc_id))
            ).scalar_one_or_none()
            tender_context = (doc.parsed_content or "")[:4000] if doc else ""
        return {
            "chapter_title": row.title,
            "original_content": row.content or "",
            "chapter_outline": json.dumps(outline.tree, ensure_ascii=False) if outline and outline.tree else "",
            "tender_context": tender_context,
            "analysis_dimensions": analysis_dimensions,
            # G7-R3：全文招标文本（供摘录抽取）；B 模式不消费 tender_context，
            # 修复所需的预算/工期/质保等要件值经 extra_docs 台账注入。
            "tender_text": (doc.parsed_content or "") if doc else "",
        }


async def repair_chapter(project_id: str, task: dict, llm: Any = None) -> dict:
    """单章修复：B 模式重写 + 硬门 + 落库。返回带前后数字的修复结果。"""
    chapter_id = str(task.get("chapter_id") or "")
    started = time.monotonic()
    context = await _load_chapter_context(project_id, chapter_id)
    if context is None:
        return {"chapter_id": chapter_id, "ok": False, "error": f"章节不存在: {chapter_id}"}

    directive = _REPAIR_DIRECTIVE.format(
        finding=task.get("finding", ""),
        tender_basis=task.get("tender_basis", ""),
        suggestion=task.get("suggestion", ""),
    )
    from core.agent_engine.generate_graph import (
        get_default_knowledge_base_cached,
        run_gate_node,
        run_persist_node,
    )
    from core.skill_engine.base import SkillContext
    from services.generate.skills.content_gen_skill import ContentGenSkill
    from services.llm_factory import get_llm_gateway

    if llm is None:
        llm = get_llm_gateway()
    knowledge_base = await get_default_knowledge_base_cached()
    # G7-5：重写前按缺陷定向检索企业库，事实以 extra_docs 进入引用台账（可过硬门），
    # 并在指令中显式要求消费编号/金额等硬事实——解决"事实可达但生成不消费"。
    fact_docs, facts_text = await _retrieve_repair_facts(knowledge_base, task, context["chapter_title"])
    project_docs: list[dict] = []
    project_facts_text = ""
    try:
        from core.rag_engine.project_evidence import retrieve as retrieve_project_evidence

        project_docs = await retrieve_project_evidence(
            project_id,
            " ".join(
                str(part)
                for part in (
                    task.get("finding"),
                    task.get("suggestion"),
                    task.get("tender_basis"),
                    context["chapter_title"],
                )
                if part
            ),
            top_k=6,
        )
        project_facts_text = "\n\n".join(
            f"[project_fact{i}] source={(doc.get('metadata') or {}).get('source', '项目补充资料')}:\n"
            f"{str(doc.get('text') or '')[:600]}"
            for i, doc in enumerate(project_docs, start=1)
            if str(doc.get("text") or "").strip()
        )
    except Exception as exc:  # noqa: BLE001 - 项目 RAG 不可用时沿用全局库降级
        logger.warning("项目补充资料 RAG 检索失败（降级为全局企业库）: %s", exc)
    # G7-R3：招标摘录（预算/工期/质保等要件值）同样入台账，占位符才有据可填。
    tender_docs = _extract_tender_facts(context.get("tender_text", ""), task)
    kb_ent_hits = sum(1 for d in fact_docs if str((d.get("metadata") or {}).get("collection", "")).startswith("kb_ent"))
    project_evidence_hits = len(project_docs)
    extra_docs = (project_docs + fact_docs + tender_docs)[:8]
    directive = _REPAIR_DIRECTIVE.format(
        finding=task.get("finding", ""),
        tender_basis=task.get("tender_basis", ""),
        suggestion=task.get("suggestion", ""),
    ) + _PLACEHOLDER_DIRECTIVE
    # Worker J：与生成图同口径注入解读已确认要素（采购人/项目名等占位符填写源）
    from core.agent_engine.fact_directive import build_project_brief

    directive += build_project_brief(context.get("analysis_dimensions"))
    if facts_text:
        directive += _FACT_DIRECTIVE.format(
            query=" ".join(str(p) for p in (task.get("finding"), task.get("suggestion")) if p)[:200],
            facts=facts_text,
        )
    if project_facts_text:
        directive += _FACT_DIRECTIVE.format(
            query="项目补充资料（本项目 RAG）",
            facts=project_facts_text,
        )
    skill = ContentGenSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=None,
        llm=llm,
        knowledge_base=knowledge_base,
        parameters={
            "mode": "B" if knowledge_base is not None else "A",
            "chapter_title": context["chapter_title"],
            "chapter_outline": (context["chapter_outline"] or "") + directive,
            "tender_context": context["tender_context"],
            "extra_docs": extra_docs,
            "word_count": 3000,
            "enable_illustration": False,
            "grounding_mode": "defer",
        },
    )
    skill_result = await skill.safe_execute(ctx)
    if not skill_result.success or not skill_result.data:
        return {
            "chapter_id": chapter_id,
            "ok": False,
            "error": str(skill_result.error or "")[:300],
            "seconds": round(time.monotonic() - started, 3),
        }
    data = dict(skill_result.data)
    # Deterministic last mile: the model may draft prose, but project facts
    # must win before the chapter reaches the grounding gate.  Merge retrieved
    # evidence into the ledger first so the same canonicalization works for
    # every project and every chapter type.
    ledger_raw = dict(data.get("ledger_raw") or {})
    next_anchor = max((int(k) for k in ledger_raw if str(k).isdigit()), default=0) + 1
    for evidence in extra_docs:
        evidence_text = str(evidence.get("text") or "").strip()
        if not evidence_text:
            continue
        meta = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
        ledger_raw[next_anchor] = {
            "chunk_id": str(meta.get("chunk_id") or f"project_evidence_{evidence.get('id') or next_anchor}"),
            "text": evidence_text,
            "excerpt": evidence_text[:200],
            "source": str(meta.get("source") or meta.get("file_name") or "项目补充资料"),
            "collection": str(meta.get("collection") or "kb_proj_" + project_id.replace("-", "")),
        }
        next_anchor += 1
    data["ledger_raw"] = ledger_raw
    try:
        from core.agent_engine.fact_harmonizer import harmonize_content

        harmonized, harmonize_meta = harmonize_content(
            str(data.get("content") or ""), context["chapter_title"], ledger_raw
        )
        data["content"] = harmonized
        data["fact_harmonization"] = harmonize_meta
    except Exception as exc:  # noqa: BLE001 - drafting remains available if a helper degrades
        logger.warning("正文事实协调失败（保留草稿并交给 grounding gate）: %s", exc)
    # RAG 证据必须真正落入对应投标章节，不能只停留在 prompt/台账里。
    # 正文只写用户可交付的附件名称与事实摘要；collection、RAG、source 等
    # 内部检索元数据留在 ledger，不得污染正式投标文件。
    # Project uploads are the current, bid-specific source of truth.  Global
    # enterprise KB hits remain available to the writer through the evidence
    # ledger, but must not be copied into the formal chapter appendix: doing
    # so can reintroduce an older price/deposit version that the project RAG
    # has already superseded.
    material_docs = (project_docs or fact_docs)[:8]
    # Only attachment/qualification chapters receive a concise manifest.  The
    # evidence text itself remains in the citation ledger; copying raw RAG
    # chunks into every chapter creates duplicate content and confuses later
    # integrity checks.  This semantic mapping is project-agnostic.
    if material_docs and re.search(r"资质|资格|证明|附件|授权|营业执照|人员|业绩", context["chapter_title"]):
        annex_lines = ["\n\n### 证明材料与附件清单"]
        seen_sources: set[str] = set()
        for index, doc in enumerate(material_docs, start=1):
            meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            source = str(meta.get("source") or meta.get("file_name") or "企业证明材料").strip()
            if not source or source in seen_sources:
                continue
            seen_sources.add(source)
            annex_lines.append(f"{len(seen_sources)}. {source}")
        if len(annex_lines) > 1:
            data["content"] = str(data.get("content") or "") + "\n".join(annex_lines)
    # The annex manifest is appended after the first draft. Run the same
    # deterministic harmonizer once more so late-added material lines cannot
    # reintroduce stale amounts, discount rows, policy claims, or OCR debris.
    try:
        from core.agent_engine.fact_harmonizer import harmonize_content

        harmonized, harmonize_meta = harmonize_content(
            str(data.get("content") or ""), context["chapter_title"], ledger_raw
        )
        data["content"] = harmonized
        data["fact_harmonization"] = harmonize_meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("附件清单追加后的事实协调失败（保留正文）: %s", exc)
    chapter_current = {
        "id": chapter_id,
        "title": context["chapter_title"],
        "mode": "B",
        "skill_mode": "B" if knowledge_base is not None else "A",
        "unified_injected": knowledge_base is not None,
        "fact_hits": len(fact_docs),
        "project_evidence_hits": project_evidence_hits,
        "kb_ent_hits": kb_ent_hits,
        "tender_fact_hits": len(tender_docs),
        "content_raw": str(data.get("content", "") or ""),
        "ledger_raw": dict(data.get("ledger_raw") or {}),
        "fact_harmonization": dict(data.get("fact_harmonization") or {}),
        "grounding_r1": dict(data.get("grounding") or {}),
        "grounding_detail": dict(data.get("grounding_detail") or {}),
        "citation_rate": data.get("citation_rate"),
        "seconds": round(time.monotonic() - started, 3),
    }
    # Grounding 硬门：修复稿不豁免（确定性校验+一次修正+降级兜底）
    gate_out = await run_gate_node({"chapter_current": chapter_current}, llm)
    gated = dict((gate_out.get("chapter_gated") or {}))
    if not gated:
        return {
            "chapter_id": chapter_id,
            "ok": False,
            "error": "硬门未产出章节",
            "seconds": round(time.monotonic() - started, 3),
        }
    persist_out = await run_persist_node({"project_id": project_id, **gate_out})
    records = persist_out.get("chapters_generated") or []
    record = records[0] if records else {}
    return {
        "chapter_id": chapter_id,
        "ok": record.get("status") == "generated",
        "gate_verdict": gated.get("gate_verdict"),
        "grounding_after": gated.get("grounding_after"),
        "grounding_before": gated.get("grounding_before"),
        "word_count": record.get("word_count"),
        "ledger_persisted": record.get("ledger_persisted"),
        "fact_hits": len(fact_docs),
        "kb_ent_hits": kb_ent_hits,
        "tender_fact_hits": len(tender_docs),
        "seconds": round(time.monotonic() - started, 3),
        "error": record.get("error", ""),
    }


async def recheck_item(project_id: str, task: dict, llm: Any = None) -> dict:
    """相关项复检：按 check_id 对修复后的全文重跑该单项（bid_text 落库后重拼，即含修复稿）。"""
    from sqlalchemy import select

    from services.check.graph_adapter import run_all_checks
    from services.database import async_session
    from services.llm_factory import get_llm_gateway
    from services.models import Chapter as ChapterRow
    from services.models import Document, Project

    if llm is None:
        llm = get_llm_gateway()
    check_id = str(task.get("check_id") or "")
    async with async_session()() as db:
        project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
        tender_text = ""
        if project and project.tender_doc_id:
            doc = (
                await db.execute(select(Document).where(Document.id == project.tender_doc_id))
            ).scalar_one_or_none()
            tender_text = doc.parsed_content or "" if doc else ""
        chapters = (
            await db.execute(select(ChapterRow).where(ChapterRow.project_id == project_id))
        ).scalars().all()
        bid_text = "\n\n".join(f"## {ch.title}\n{ch.content or ''}" for ch in chapters if ch.content)
        from core.agent_engine.export_sanitizer import sanitize_export_text

        bid_text, _ = sanitize_export_text(bid_text)
    # Recheck must use the same current-evidence reconciliation as the main
    # graph check. Without this, an older persisted chapter can continue to
    # report stale price/representative facts even after new RAG evidence was
    # uploaded. The helper is generic and only rewrites semantically matching
    # facts; it does not alter graph stages or decision semantics.
    try:
        from core.agent_engine.master_graph import load_bid_text_if_missing

        reconciled = await load_bid_text_if_missing(project_id, "")
        if reconciled.strip():
            bid_text = reconciled
    except Exception:  # noqa: BLE001 - recheck remains available on degraded storage
        pass
    supplemental_evidence = ""
    try:
        from core.rag_engine.project_evidence import retrieve

        evidence = await retrieve(project_id, f"项目补充资料 {check_id} {task.get('finding', '')}", top_k=12)
        snippets = [
            f"【RAG补充证据：{(item.get('metadata') or {}).get('source', '项目资料')}】\n"
            f"{str(item.get('text') or '')[:1200]}"
            for item in evidence
            if str(item.get("text") or "").strip()
        ]
        if snippets:
            supplemental_evidence = "\n\n".join(snippets)
    except Exception:  # noqa: BLE001
        pass
    results = await run_all_checks(
        tender_text=tender_text,
        bid_text=bid_text,
        llm=llm,
        project_id=project_id,
        check_ids=[check_id] if check_id else None,
        extra_params={"supplemental_evidence": supplemental_evidence},
    )
    row = results[0] if results else {}
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    return {
        "status": str(row.get("status", "skipped")),
        "reason": str(row.get("reason", ""))[:300],
        # G7-R3：复检明细（供 finding 指纹比对，计算 finding 级解决率）
        "checks": [
            {
                "check_name": str(c.get("check_name") or ""),
                "value_a": str(c.get("value_a") or "")[:200],
                "value_b": str(c.get("value_b") or "")[:200],
            }
            for c in (data.get("checks") if isinstance(data.get("checks"), list) else [])
            if isinstance(c, dict) and c.get("status") in ("fail", "warning")
        ],
    }


async def production_repair_runner(state: dict, repair_queue: list[dict], llm: Any = None) -> dict:
    """CheckGraphOrchestrator.repair_runner 签名：单轮修复，任务量上限可配（settings.repair_max_tasks）。"""
    from core.settings import get_settings
    from services.check.feedback_loop import run_repair_queue

    cap = get_settings().repair_max_tasks
    queue = list(repair_queue or [])
    if cap is not None and int(cap) >= 0:
        queue = queue[: int(cap)]
    project_id = str(state.get("project_id") or "")
    if not project_id:
        return {"total": 0, "fixed": 0, "recheck_pass_rate": 1.0, "tasks": [], "error": "project_id 缺失"}

    async def repair(task: dict) -> dict:
        try:
            return await repair_chapter(project_id, task, llm)
        except Exception as exc:  # noqa: BLE001
            logger.exception("repair task failed: %s", task.get("task_id"))
            return {"chapter_id": task.get("chapter_id"), "ok": False, "error": str(exc)[:300]}

    async def recheck(task: dict) -> dict:
        try:
            return await recheck_item(project_id, task, llm)
        except Exception as exc:  # noqa: BLE001
            logger.exception("recheck task failed: %s", task.get("task_id"))
            return {"status": "error", "reason": str(exc)[:300]}

    # G7-5：事实型缺陷（要求物理盖章/原件等线下行为）文本重写无法根治，标记后单独统计
    # 有效口径（text_recheck_pass_rate 只算文本可修复项）。
    for task in queue:
        finding_text = str(task.get("finding", "")) + str(task.get("suggestion", ""))
        task["fact_required"] = bool(_FACTUAL_FINDING_RE.search(finding_text))

    feedback = await run_repair_queue(queue, repair, recheck)

    # G7-R3：finding 级解决率——原始 finding 指纹（check_name+value_a）在复检明细中
    # 不再出现即视为已解决。状态口径（recheck 整项 pass 才算 fixed）保持不变，
    # finding 级口径给出更细的有效数字（跨表一致性单项很难整项全过）。
    findings_total = 0
    findings_resolved = 0
    for t in feedback.get("tasks", []):
        refs = t.get("finding_refs") or []
        checks_after = (t.get("recheck") or {}).get("checks") or []
        resolved = 0
        for ref in refs:
            still = any(
                (not ref.get("check_name") or c.get("check_name") == ref["check_name"])
                and (not ref.get("value_a") or c.get("value_a") == ref["value_a"])
                for c in checks_after
            )
            findings_total += 1
            if not still:
                resolved += 1
        t["findings_total"] = len(refs)
        t["findings_resolved"] = resolved
        findings_resolved += resolved
    feedback["findings_total"] = findings_total
    feedback["findings_resolved"] = findings_resolved
    feedback["finding_resolution_rate"] = (findings_resolved / findings_total) if findings_total else None

    text_rows = [t for t in feedback.get("tasks", []) if not t.get("fact_required")]
    text_fixed = sum(1 for row in text_rows if row.get("fixed"))
    feedback["text_total"] = len(text_rows)
    feedback["text_fixed"] = text_fixed
    feedback["text_recheck_pass_rate"] = (text_fixed / len(text_rows)) if text_rows else None
    feedback["fact_required_total"] = sum(1 for t in feedback.get("tasks", []) if t.get("fact_required"))
    logger.info(
        "G-6 repair loop: total=%s fixed=%s rate=%s",
        feedback.get("total"),
        feedback.get("fixed"),
        feedback.get("recheck_pass_rate"),
    )
    return feedback


__all__ = ["production_repair_runner", "repair_chapter", "recheck_item"]
