"""P-G G-2：章节生成入图（大纲/正文 A/B 成图节点，吸收 content_gen_skill 编排权）。

拓扑（每章一个图步 = 每章一个 checkpoint，kill 后从最近 checkpoint 续写）：

    大纲生成(outline_gen) → 章节生成(chapter_gen) → Grounding 硬门(grounding_gate)
      → 章节落库(chapter_persist) ──仍有未完成章──→ chapter_gen（循环）
                                    └──全部完成──→ generate_finalize

语义约束：
- 能力复用不重写：大纲节点复用 OutlineGenSkill（经 generate.py 既有任务逻辑）；
  正文节点复用 ContentGenSkill（B 模式【n】锚点+citation_ledger+grounding 语义原样保留）；
- A/B 统一注入：图路径正文一律检索注入（kb 有据时统一走 B 管线，消灭 A 薄壳；
  chapter.mode 仍记录请求模式，记录 unified_injected=True）；
- Grounding 硬门：正文过 grounding_hard_gate（确定性校验+一次修正重生成+降级），
  门级放行/拒收/降级统计进 run 快照；
- Chapter 表写入逻辑不变（content/mode/status/word_count + 新增 citation_ledger 落库）；
- 全部节点禁 ReAct（铁律 2）；LLM 调用仅限 outline/chapter/门修正通道；
- 图状态全部 JSON 可序列化（PG checkpoint 兼容）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from core.agent_engine.grounding_hard_gate import (
    VERDICT_DEGRADED,
    build_revise_messages,
    gate_pass_rate,
    run_hard_gate,
)

logger = logging.getLogger(__name__)

# 图拓扑节点名（固定，防漂移；不复用铁律常量集合，避免影响 P-D1 断言）
NODE_OUTLINE = "outline_gen"
NODE_CHAPTER = "chapter_gen"
NODE_GATE = "grounding_gate"
NODE_PERSIST = "chapter_persist"
NODE_FINALIZE = "generate_finalize"
GENERATE_GRAPH_NODES: tuple[str, ...] = (NODE_OUTLINE, NODE_CHAPTER, NODE_GATE, NODE_PERSIST, NODE_FINALIZE)


def merge_dict(left: dict | None, right: dict | None) -> dict:
    base = dict(left or {})
    base.update(right or {})
    return base


class GenerationGraphState(TypedDict, total=False):
    """章节生成图状态（全部 JSON 可序列化值，便于 PG checkpoint）。"""

    # 输入
    run_id: str
    project_id: str
    outline_mode: str  # 大纲生成模式（aligned/default），透传 OutlineGenSkill
    run_outline: bool  # 是否在本 run 中生成大纲（默认 True；已有大纲可关）
    outline_only: bool  # True=只跑大纲到终态（不进章节循环）；大项目大纲入口用，防 runaway 全量正文
    chapter_modes: dict  # {chapter_id: "A"|"B"}；未指定的章节默认 "B"
    chapter_ids: list  # 限定要生成的章节（缺省=全部大纲章节）

    # 节点产出
    outline_result: dict  # 大纲节点执行结果（success/data/error）
    chapters_plan: list  # [{id, title, mode}]（本次 run 要生成的章节队列）
    chapter_current: dict  # 当前章生成产物（chapter_gen → gate → persist 传递）
    chapter_gated: dict  # 当前章门后产物（gate → persist 传递）
    chapters_generated: Annotated[list[dict], operator.add]  # 每章终态记录（追加式）
    pending_more: bool  # persist 节点路由信号：仍有未完成章

    # 记账/观测
    node_status: Annotated[dict[str, str], merge_dict]
    node_costs: Annotated[dict[str, dict], merge_dict]
    timing: Annotated[dict, merge_dict]  # {outline_seconds, chapters:{id: seconds}}
    grounding_summary: Annotated[dict, merge_dict]  # {total, passed, degraded, rejected} 聚合
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    current_stage: str


# --------------------------------------------------------------------------- #
# 默认知识库缓存（图进程内单例；只读检索，不碰库）
# --------------------------------------------------------------------------- #

_KB_CACHE: dict[str, Any] = {}
_KB_LOCK = asyncio.Lock()


async def get_default_knowledge_base_cached() -> Any:
    """进程内缓存默认知识库（kb_adapter 双库入口），避免逐章重建。"""
    async with _KB_LOCK:
        if "kb" not in _KB_CACHE:
            from core.rag_engine.kb_adapter import build_default_knowledge_base

            _KB_CACHE["kb"] = await build_default_knowledge_base()
        return _KB_CACHE["kb"]


def reset_kb_cache() -> None:
    """测试/重置用。"""
    _KB_CACHE.clear()


# --------------------------------------------------------------------------- #
# 类型化节点
# --------------------------------------------------------------------------- #


async def run_outline_node(state: dict) -> dict:
    """大纲生成节点：复用 generate.py 既有任务逻辑（OutlineGenSkill+chapters 物化不变）。"""
    started = time.monotonic()
    project_id = str(state.get("project_id") or "")
    # P1（G-6 验收发现）：outline_only 语义=“确保大纲/计划就绪后直达终态”。
    # 库内已有大纲时禁止重建——重建会重置章节计划、丢失已生成正文
    # （UAT-0831 实证 141 章计划被重置、65 章生成内容丢失）。
    if state.get("outline_only") and await _project_has_outline(project_id):
        plan, err = await _load_plan_from_db(project_id, state)
        warnings = []
        if err:
            warnings.append(f"outline_only：库内已有大纲，跳过重建直接读取既有章节（读取失败：{err}）")
        return {
            "outline_result": {"success": not err, "data": {"outline": {}}, "error": err},
            "chapters_plan": plan,
            "timing": {"outline_seconds": round(time.monotonic() - started, 3)},
            "node_status": {NODE_OUTLINE: "skipped"},
            "current_stage": "outline_ready",
            "warnings": warnings,
        }
    if not state.get("run_outline", True):
        # 已有大纲场景：直接由 DB 读取章节构建计划（不重跑大纲）
        plan, err = await _load_plan_from_db(project_id, state)
        warnings = []
        if err:
            warnings.append(f"未重跑大纲，直接读取既有章节：{err}")
        return {
            "outline_result": {"success": not err, "data": {"outline": {}}, "error": err},
            "chapters_plan": plan,
            "timing": {"outline_seconds": round(time.monotonic() - started, 3)},
            "node_status": {NODE_OUTLINE: "skipped"},
            "current_stage": "outline_ready",
        }
    try:
        from services.routers.generate import _do_generate_outline

        result = await _do_generate_outline(project_id, str(state.get("outline_mode") or "aligned"))
    except Exception as e:  # noqa: BLE001
        return {
            "outline_result": {"success": False, "data": {}, "error": str(e)[:300]},
            "errors": [f"大纲生成异常: {e}"],
            "node_status": {NODE_OUTLINE: "error"},
            "current_stage": "outline_failed",
        }
    if not result.get("success"):
        return {
            "outline_result": dict(result),
            "errors": [f"大纲生成失败: {result.get('error', '')}"],
            "node_status": {NODE_OUTLINE: "error"},
            "current_stage": "outline_failed",
        }
    plan, err = await _load_plan_from_db(project_id, state)
    errors = []
    if err:
        errors.append(f"章节物化读取失败: {err}")
    if not plan:
        errors.append("大纲章节为空，无正文可生成")
    return {
        "outline_result": dict(result),
        "chapters_plan": plan,
        "timing": {"outline_seconds": round(time.monotonic() - started, 3)},
        "node_status": {NODE_OUTLINE: "done" if plan else "error"},
        "current_stage": "outline_ready" if plan else "outline_failed",
        "errors": errors,
    }


async def _project_has_outline(project_id: str) -> bool:
    """库内是否已存在大纲（outline_only 防重建保护用，G-6 P1）。"""
    from sqlalchemy import select

    from services.database import async_session
    from services.models import Outline

    try:
        async with async_session()() as db:
            row = (
                await db.execute(select(Outline.id).where(Outline.project_id == project_id).limit(1))
            ).scalar_one_or_none()
            return row is not None
    except Exception:  # noqa: BLE001
        return False


async def _load_plan_from_db(project_id: str, state: dict) -> tuple[list[dict], str]:
    """从 Chapter 表读取章节队列并叠加请求模式（写入逻辑不变，只读取计划）。"""
    from sqlalchemy import select

    from services.database import async_session
    from services.models import Chapter

    chapter_modes = dict(state.get("chapter_modes") or {})
    wanted_ids = set(str(i) for i in (state.get("chapter_ids") or []))
    plan: list[dict] = []
    try:
        async with async_session()() as db:
            rows = (
                await db.execute(select(Chapter).where(Chapter.project_id == project_id))
            ).scalars().all()
        rows.sort(key=lambda c: (c.sort_order or 0, tuple(int(s) if s.isdigit() else 0 for s in str(c.id).split("."))))
        for row in rows:
            if wanted_ids and row.id not in wanted_ids:
                continue
            plan.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "mode": str(chapter_modes.get(row.id) or "B").upper(),
                }
            )
        return plan, ""
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:300]


async def run_chapter_node(state: dict) -> dict:
    """正文生成节点：类型化窄职责，A/B 统一检索注入（B 模式管线），复用 ContentGenSkill。"""
    plan = state.get("chapters_plan") or []
    done_ids = {str(r.get("id")) for r in (state.get("chapters_generated") or [])}
    pending = [c for c in plan if str(c.get("id")) not in done_ids]
    if not pending:
        return {
            "chapter_current": {},
            "pending_more": False,
            "node_status": {NODE_CHAPTER: "skipped"},
            "current_stage": "chapters_done",
        }

    chapter = pending[0]
    project_id = str(state.get("project_id") or "")
    requested_mode = str(chapter.get("mode") or "B").upper()
    started = time.monotonic()

    # 取章节/大纲/招标上下文（与路由同口径只读）
    from sqlalchemy import select

    from services.database import async_session
    from services.models import Analysis, Document, Outline, Project
    from services.models import Chapter as ChapterRow

    try:
        async with async_session()() as db:
            row = (
                await db.execute(
                    select(ChapterRow).where(ChapterRow.id == chapter["id"], ChapterRow.project_id == project_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return {
                    "pending_more": False,
                    "errors": [f"章节不存在: {chapter['id']}"],
                    "node_status": {NODE_CHAPTER: "error"},
                    "current_stage": "chapter_failed",
                }
            # kill-resume 幂等：已生成章不重写（DB 状态为真源）
            if (row.status or "") == "generated" and row.content:
                done_ids.add(row.id)
                ledger = dict(row.citation_ledger or {})
                source_ledger = {key: value for key, value in ledger.items() if not str(key).startswith("_")}
                recovered = {
                    "id": row.id,
                    "title": row.title,
                    "mode": str(row.mode or requested_mode),
                    "status": "generated",
                    "word_count": int(row.word_count or len(row.content or "")),
                    "ledger_count": len(source_ledger),
                    "ledger_persisted": bool(ledger),
                    "seconds": 0.0,
                    "recovered_from_db": True,
                }
                remaining = [c for c in plan if str(c.get("id")) not in done_ids]
                return {
                    "chapter_current": {},
                    "chapters_generated": [recovered],
                    "pending_more": bool(remaining),
                    "node_status": {NODE_CHAPTER: "skipped"},
                    "current_stage": "chapters_running" if remaining else "chapters_done",
                }
            project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            outline = (await db.execute(select(Outline).where(Outline.project_id == project_id))).scalar_one_or_none()
            tender_context = ""
            if project and project.tender_doc_id:
                doc = (
                    await db.execute(select(Document).where(Document.id == project.tender_doc_id))
                ).scalar_one_or_none()
                tender_context = (doc.parsed_content or "")[:4000] if doc else ""
            chapter_outline = json.dumps(outline.tree, ensure_ascii=False) if outline and outline.tree else ""
            # Worker J（净化层缺陷③根因）：读解读结果 dimensions——采购人/项目名
            # 等已确认要素过去从未进章节生成上下文，"致：[采购人名称]"无从填写。
            analysis = (
                await db.execute(select(Analysis).where(Analysis.project_id == project_id))
            ).scalar_one_or_none()
            analysis_dimensions = analysis.dimensions if analysis and isinstance(analysis.dimensions, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {
            "pending_more": False,
            "errors": [f"章节读取失败: {e}"],
            "node_status": {NODE_CHAPTER: "error"},
            "current_stage": "chapter_failed",
        }

    # A/B 统一：检索注入（kb 有据 → 统一 B 管线；无据 → skill 内部回退 A 提示语义）
    knowledge_base = await get_default_knowledge_base_cached()
    unified_injected = knowledge_base is not None

    # G-7 收尾根治：B 模式检索改定向查询（章节标题+该章小节标题，去掉全大纲树 JSON 稀释），
    # 企业事实以 extra_docs 进台账，指令要求消费硬事实带【n】锚点——与 repair 路径三件套同口径。
    from core.agent_engine.fact_directive import (
        PLACEHOLDER_DIRECTIVE,
        build_chapter_retrieval_query,
        build_fact_directive,
        build_project_brief,
        retrieve_generation_facts,
    )

    retrieval_query = build_chapter_retrieval_query(row.id, row.title, outline.tree if outline else None)
    fact_docs, facts_text = await retrieve_generation_facts(knowledge_base, retrieval_query)
    project_fact_docs: list[dict] = []
    try:
        from core.rag_engine.project_evidence import retrieve as retrieve_project_evidence

        project_fact_docs = await retrieve_project_evidence(project_id, retrieval_query, top_k=6)
    except Exception:  # noqa: BLE001 - 项目 RAG 不可用时沿用企业库
        project_fact_docs = []
    # 项目补充资料是本项目最新口径，优先于全局企业库；全局库仍作为通用能力兜底。
    if project_fact_docs:
        fact_docs = (project_fact_docs + fact_docs)[:8]
        facts_text = "\n\n".join(
            f"[fact{i}] collection={(doc.get('metadata') or {}).get('collection', 'kb_proj')} "
            f"source={(doc.get('metadata') or {}).get('source', '')}:\n{str(doc.get('text') or '')[:600]}"
            for i, doc in enumerate(fact_docs, start=1)
            if str(doc.get("text") or "").strip()
        )
    kb_ent_hits = sum(
        1 for d in fact_docs if str((d.get("metadata") or {}).get("collection", "")).startswith("kb_ent")
    )
    fact_directive = build_fact_directive(retrieval_query, facts_text)
    project_brief = build_project_brief(analysis_dimensions)
    chapter_outline_effective = (chapter_outline or "") + project_brief + PLACEHOLDER_DIRECTIVE + fact_directive
    if project_fact_docs:
        chapter_outline_effective += (
            "\n【本项目补充资料优先级】以下项目 RAG 证据优先于全局企业库；若两者冲突，"
            "以本项目最新补充资料为准，并在正文中只保留一个一致口径。\n"
        )

    from core.skill_engine.base import SkillContext
    from services.generate.skills.content_gen_skill import ContentGenSkill
    from services.llm_factory import get_llm_gateway

    skill_mode = "B" if (unified_injected and requested_mode == "A") else requested_mode
    gateway = get_llm_gateway()
    skill = ContentGenSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=None,
        llm=gateway,
        knowledge_base=knowledge_base,
        parameters={
            "mode": skill_mode,
            "chapter_title": row.title,
            "chapter_outline": chapter_outline_effective,
            "retrieval_query": retrieval_query,
            "extra_docs": fact_docs[:8],
            "tender_context": tender_context,
            "word_count": 3000,
            # 图路径关闭配图（闸门/模板只消费正文；与直调路径行为差异记录在快照 warnings）
            "enable_illustration": False,
            # Grounding 内联替换交给图内硬门（defer）：门做确定性校验+修正+降级
            "grounding_mode": "defer",
        },
    )
    skill_result = await skill.safe_execute(ctx)
    seconds = round(time.monotonic() - started, 3)
    if not skill_result.success or not skill_result.data:
        return {
            "pending_more": False,
            "failed": True,
            "chapters_generated": [
                {
                    "id": row.id,
                    "title": row.title,
                    "mode": requested_mode,
                    "status": "failed",
                    "error": str(skill_result.error or "")[:300],
                    "seconds": seconds,
                }
            ],
            "node_status": {NODE_CHAPTER: "error"},
            "current_stage": "chapters_running",
        }
    data = dict(skill_result.data)
    ledger_raw = dict(data.get("ledger_raw") or {})
    return {
        "chapter_current": {
            "id": row.id,
            "title": row.title,
            "mode": requested_mode,
            "skill_mode": skill_mode,
            "unified_injected": unified_injected,
            "retrieval_query": retrieval_query,
            "fact_hits": len(fact_docs),
            "kb_ent_hits": kb_ent_hits,
            "content_raw": str(data.get("content", "") or ""),
            "ledger_raw": ledger_raw,
            "grounding_r1": dict(data.get("grounding") or {}),
            "grounding_detail": dict(data.get("grounding_detail") or {}),
            "citation_rate": data.get("citation_rate"),
            "sources": data.get("sources") or [],
            "seconds": seconds,
        },
        "node_status": {NODE_CHAPTER: "done"},
        "current_stage": "chapters_running",
        "timing": {"chapters": {row.id: seconds}},
        "errors": [],
    }


def run_finalize_node(state: dict) -> dict:
    """终态：聚合章节数字（耗时/锚点/ledger/门通过率），供快照与 E2E 出数。"""
    generated = state.get("chapters_generated") or []
    ok = [r for r in generated if r.get("status") == "generated"]
    failed = [r for r in generated if r.get("status") == "failed"]
    total_seconds = round(sum(float(r.get("seconds") or 0) for r in ok), 3)
    return {
        "current_stage": "failed" if failed or state.get("failed") else "finalized",
        "node_status": {NODE_FINALIZE: "error" if failed or state.get("failed") else "done"},
        "timing": {"chapters_total_seconds": total_seconds, "chapters_generated": len(ok)},
    }


def _route_after_chapter_gen(state: dict) -> str:
    """chapter_gen：有产物 → 门；无产物但仍有待生成章 → 下一章；否则终态。"""
    if state.get("chapter_current"):
        return NODE_GATE
    return NODE_CHAPTER if state.get("pending_more") else NODE_FINALIZE


async def run_gate_node(state: dict, llm: Any) -> dict:
    """Grounding 硬门节点：确定性校验 + 一次修正重生成（建议【n】确定性反查）+ 降级兜底。"""
    current = dict(state.get("chapter_current") or {})
    if not current:
        return {"node_status": {NODE_GATE: "skipped"}}
    # ledger 键归一化为 int（checkpoint JSON 往返可能把 int 键转成 str，判定统一按 int）
    ledger: dict = {}
    for k, v in dict(current.get("ledger_raw") or {}).items():
        try:
            ledger[int(k)] = dict(v)
        except (TypeError, ValueError):
            continue
    content = str(current.get("content_raw") or "")

    async def revise(text: str, hints: list[dict]) -> str:
        messages = build_revise_messages(text, hints, ledger)
        resp = await llm.chat(messages=messages, temperature=0.2)
        content_out = getattr(resp, "content", None)
        return str(content_out if content_out is not None else resp or "")

    gate = await run_hard_gate(content, ledger, revise=revise if ledger else None)
    rates = gate_pass_rate(gate["before"], gate["after"])
    verdict = gate["verdict"]
    # G-7 否决修复①：配图脚手架禁止进最终正文——门后确定性剥离为侧栏元数据
    # Worker J（缺陷③）：扩展变体（此处可插入/[此处插入…]/建议插入…/建议配图描述块）
    from core.agent_engine.illustration_guard import strip_illustration_scaffold_extended

    gate_text_raw = str(gate.get("text") or "")
    # G-7 收尾：AI 配图增量（备用状态）——开关开+key 可用时，先在标记位原位装配图片引用，
    # 再剥离残余脚手架；开关关=现状（建议语剥离存侧栏）。
    illustration_images: list[dict] = []
    illustration_status = "disabled"
    try:
        from core.agent_engine.illustration_assembly import (
            assemble_illustrations,
            get_illustration_params,
        )

        illu_params = get_illustration_params()
        if illu_params is not None:
            gate_text_raw, illustration_images, illustration_status = await assemble_illustrations(
                str(state.get("project_id") or ""), gate_text_raw, [], illu_params
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("配图装配失败（降级为不装配）: %s", e)
        illustration_status = f"error: {e}"
    gate_text, illustration_suggestions = strip_illustration_scaffold_extended(gate_text_raw)
    gated = {
        "id": current.get("id"),
        "title": current.get("title"),
        "mode": current.get("mode"),
        "skill_mode": current.get("skill_mode"),
        "unified_injected": current.get("unified_injected"),
        "content": gate_text,
        "illustration_suggestions": illustration_suggestions,
        "illustration_images": illustration_images,
        "illustration_status": illustration_status,
        "gate_verdict": verdict,
        "gate_rounds": gate["rounds"],
        "grounding_before": gate["before"],
        "grounding_after": gate["after"],
        "gate_rates": rates,
        "degraded_count": len(gate["degraded"] or []),
        "anchor_audit": gate["anchor_audit"],
        "ledger_count": len(ledger),
        "ledger_raw": ledger,
        "seconds": current.get("seconds", 0.0),
    }
    # 门后引用有效率（final 文本 × eval 同源校验，口径同直调路径）
    try:
        from core.agent_engine.evidence_gate import ledger_texts, make_ledger_anchor_func
        from eval.metrics.citation import citation_valid_rate

        gated["citation_rate"] = citation_valid_rate(
            gate["text"], ledger_texts(ledger), anchor_func=make_ledger_anchor_func(ledger)
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("门后引用有效率计算失败: %s", e)
        gated["citation_rate"] = current.get("citation_rate")
    return {
        "chapter_gated": gated,
        "grounding_summary": {
            "total": int(gate["after"].get("total", 0) or 0),
            "passed": int(gate["after"].get("passed", 0) or 0),
            "degraded": int(gate["after"].get("degraded", 0) or 0),
            "rejected": 0,
        },
        "node_status": {NODE_GATE: "done"},
        "warnings": (
            [f"Grounding 门降级 {len(gate['degraded'])} 处硬事实（已标待补充）"] if verdict == VERDICT_DEGRADED else []
        ),
    }


async def run_persist_node(state: dict) -> dict:
    """章节落库节点：Chapter 表写入逻辑不变 + citation_ledger 落库（G-2 条目 3）。"""
    gated = dict(state.get("chapter_gated") or {})
    project_id = str(state.get("project_id") or "")
    if not gated:
        return {"pending_more": False, "node_status": {NODE_PERSIST: "skipped"}}
    from sqlalchemy import select

    from core.agent_engine.evidence_gate import ledger_for_output
    from services.database import async_session
    from services.models import Chapter as ChapterRow
    from services.models import Project, ProjectStatus

    record = {
        "id": gated.get("id"),
        "title": gated.get("title"),
        "mode": gated.get("mode"),
        "unified_injected": gated.get("unified_injected"),
        "gate_verdict": gated.get("gate_verdict"),
        "gate_rounds": gated.get("gate_rounds"),
        "grounding_before": gated.get("grounding_before"),
        "grounding_after": gated.get("grounding_after"),
        "gate_rates": gated.get("gate_rates"),
        "degraded_count": gated.get("degraded_count"),
        "anchors": gated.get("anchor_audit", {}).get("anchors", []),
        "anchor_audit": gated.get("anchor_audit", {}),
        "ledger_count": gated.get("ledger_count", 0),
        "word_count": len(str(gated.get("content") or "")),
        "seconds": gated.get("seconds", 0.0),
        "status": "generated",
        # G-7 否决修复①：剥离的配图脚手架作为侧栏元数据随引用台账落库（不进正文）
        "illustration_suggestions": list(gated.get("illustration_suggestions") or []),
    }
    warnings = []
    try:
        async with async_session()() as db:
            row = (
                await db.execute(
                    select(ChapterRow).where(ChapterRow.id == gated.get("id"), ChapterRow.project_id == project_id)
                )
            ).scalar_one_or_none()
            if row is None:
                record["status"] = "failed"
                return {
                    "chapters_generated": [record],
                    "pending_more": False,
                    "errors": [f"章节行缺失，落库失败: {gated.get('id')}"],
                    "node_status": {NODE_PERSIST: "error"},
                    "current_stage": "chapter_failed",
                }
            row.content = str(gated.get("content") or "")
            row.mode = str(gated.get("mode") or "B")
            row.status = "generated"
            row.word_count = record["word_count"]
            # G-2 条目 3：引用对照表持久化进 Chapter 行（正文页【n】点查来源）
            persisted_ledger = ledger_for_output(dict(gated.get("ledger_raw") or {}) or {})
            if gated.get("illustration_suggestions"):
                # G-7 否决修复①：配图建议存侧栏元数据（正文已剥离）
                persisted_ledger = {**persisted_ledger, "_illustrations": list(gated["illustration_suggestions"])}
            if gated.get("illustration_images"):
                # G-7 收尾：已装配的图片元数据存侧栏（src 含 URL/data URI，供导出嵌入）
                persisted_ledger = {**persisted_ledger, "_illustration_images": list(gated["illustration_images"])}
            row.citation_ledger = persisted_ledger
            project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            if project is not None:
                project.status = ProjectStatus.GENERATING
            await db.commit()
            row = (
                await db.execute(
                    select(ChapterRow).where(ChapterRow.id == gated.get("id"), ChapterRow.project_id == project_id)
                )
            ).scalar_one_or_none()
            record["ledger_persisted"] = bool(row and row.citation_ledger)
        # 剩余待生成章（DB 状态为真源）
        async with async_session()() as db:
            plan = state.get("chapters_plan") or []
            done_ids = {str(r.get("id")) for r in (state.get("chapters_generated") or [])}
            done_ids.add(str(gated.get("id")))
            remaining = [c for c in plan if str(c.get("id")) not in done_ids]
            pending_more = bool(remaining)
    except Exception as e:  # noqa: BLE001
        record["status"] = "failed"
        record["error"] = str(e)[:300]
        pending_more = False
        return {
            "chapters_generated": [record],
            "pending_more": False,
            "errors": [f"章节落库失败: {e}"],
            "node_status": {NODE_PERSIST: "error"},
            "current_stage": "chapter_failed",
        }
    if not record.get("ledger_persisted"):
        warnings.append(f"章节 {record['id']} citation_ledger 落库为空")
    return {
        "chapters_generated": [record],
        "pending_more": pending_more,
        "node_status": {NODE_PERSIST: "done"},
        "current_stage": "chapters_running" if pending_more else "chapters_done",
        "warnings": warnings,
    }


def _route_after_gate(state: dict) -> str:
    gated = state.get("chapter_gated") or {}
    return NODE_PERSIST if gated else NODE_FINALIZE


def _route_after_persist(state: dict) -> str:
    return NODE_CHAPTER if state.get("pending_more") else NODE_FINALIZE


def _route_after_outline(state: dict) -> str:
    if state.get("outline_only"):
        return NODE_FINALIZE  # 只跑大纲：大项目防 runaway（G-5 验收发现）
    if state.get("chapters_plan"):
        return NODE_CHAPTER
    return NODE_FINALIZE


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #


class GenerationGraphOrchestrator:
    """章节生成图编排器：build / run / resume（checkpoint 续写）/ snapshot。"""

    def __init__(self, llm: Any = None, checkpointer: Any = None):
        self.llm = llm
        self.checkpointer = checkpointer
        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(GenerationGraphState)

        async def chapter_node(state: GenerationGraphState) -> dict:
            return await run_chapter_node(dict(state))

        async def gate_node(state: GenerationGraphState) -> dict:
            return await run_gate_node(dict(state), self.llm)

        async def outline_node(state: GenerationGraphState) -> dict:
            return await run_outline_node(dict(state))

        graph.add_node(NODE_OUTLINE, outline_node)
        graph.add_node(NODE_CHAPTER, chapter_node)
        graph.add_node(NODE_GATE, gate_node)

        async def persist_node(state: GenerationGraphState) -> dict:
            return await run_persist_node(dict(state))

        graph.add_node(NODE_PERSIST, persist_node)

        async def finalize_node(state: GenerationGraphState) -> dict:
            return run_finalize_node(dict(state))

        graph.add_node(NODE_FINALIZE, finalize_node)

        graph.add_edge(START, NODE_OUTLINE)
        graph.add_conditional_edges(
            NODE_OUTLINE, _route_after_outline, {NODE_CHAPTER: NODE_CHAPTER, NODE_FINALIZE: NODE_FINALIZE}
        )
        graph.add_conditional_edges(
            NODE_CHAPTER,
            _route_after_chapter_gen,
            {NODE_GATE: NODE_GATE, NODE_CHAPTER: NODE_CHAPTER, NODE_FINALIZE: NODE_FINALIZE},
        )
        graph.add_conditional_edges(
            NODE_GATE, _route_after_gate, {NODE_PERSIST: NODE_PERSIST, NODE_FINALIZE: NODE_FINALIZE}
        )
        graph.add_conditional_edges(
            NODE_PERSIST, _route_after_persist, {NODE_CHAPTER: NODE_CHAPTER, NODE_FINALIZE: NODE_FINALIZE}
        )
        graph.add_edge(NODE_FINALIZE, END)
        return graph.compile(checkpointer=self.checkpointer)

    def _config(self, run_id: str) -> dict:
        from core.settings import graph_runtime_config

        return graph_runtime_config(run_id)

    async def run(self, run_id: str, run_input: dict) -> dict:
        """启动一次章节生成图运行（跑完到终态或因异常失败）。"""
        run_input = {"run_id": run_id, **run_input}
        await self._graph.ainvoke(run_input, self._config(run_id))
        return await self.snapshot(run_id)

    async def resume(self, run_id: str) -> dict:
        """从最近 checkpoint 续写（kill 后恢复；已完成章不重写）。

        无 checkpoint 时（从未跑过）等价于空输入重跑，由路由节点自然收敛到终态。
        """
        state = await self._graph.aget_state(self._config(run_id))
        if state.values:
            await self._graph.ainvoke(None, self._config(run_id))
        return await self.snapshot(run_id)

    async def snapshot(self, run_id: str) -> dict:
        state = await self._graph.aget_state(self._config(run_id))
        values = dict(state.values or {})
        generated = values.get("chapters_generated") or []
        ok = [r for r in generated if r.get("status") == "generated"]
        grounding_before = {"total": 0, "passed": 0, "rejected": 0}
        for r in ok:
            b = r.get("grounding_before") or {}
            for k in grounding_before:
                grounding_before[k] += int(b.get(k, 0) or 0)
        grounding_after = dict(values.get("grounding_summary") or {})
        return {
            "run_id": run_id,
            "project_id": values.get("project_id", ""),
            "current_stage": values.get("current_stage", ""),
            "node_status": values.get("node_status", {}),
            "pending_gate": None,  # 本图无 HITL 门（语义位保留，口径同主图快照）
            "next_nodes": list(state.next or []),
            "outline_result": values.get("outline_result"),
            "chapters_plan": values.get("chapters_plan", []),
            "chapters": ok,
            "chapters_all": generated,
            "errors": values.get("errors", []),
            "warnings": values.get("warnings", []),
            "timing": values.get("timing", {}),
            "grounding": {"before": grounding_before, "after": grounding_after},
            "finalized": values.get("current_stage") == "finalized",
        }


async def run_full_generation_graph(run_input: dict, llm: Any = None, checkpointer: Any = None) -> dict:
    """便捷入口：全图跑完（demo/脚本用）。"""
    orchestrator = GenerationGraphOrchestrator(llm=llm, checkpointer=checkpointer)
    run_id = run_input.get("run_id") or f"grun_{int(time.time())}"
    return await orchestrator.run(run_id, run_input)


__all__ = [
    "NODE_OUTLINE",
    "NODE_CHAPTER",
    "NODE_GATE",
    "NODE_PERSIST",
    "NODE_FINALIZE",
    "GENERATE_GRAPH_NODES",
    "GenerationGraphState",
    "GenerationGraphOrchestrator",
    "run_outline_node",
    "run_chapter_node",
    "run_gate_node",
    "run_persist_node",
    "run_finalize_node",
    "run_full_generation_graph",
    "get_default_knowledge_base_cached",
    "reset_kb_cache",
]
