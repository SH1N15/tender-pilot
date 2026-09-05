"""G-3 检查图：dispatch -> execute -> report -> export。

检查 skill 仍由既有实现提供，图节点负责生产编排、报告落库和确定性导出。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from services.check.graph_adapter import CHECK_REGISTRY, run_all_checks


class CheckGraphState(TypedDict, total=False):
    run_id: str
    project_id: str
    tender_text: str
    bid_text: str
    check_ids: list[str] | None
    formats: list[str]
    project_name: str
    results: list[dict]
    report: dict
    exports: dict[str, str]
    repair_queue: list[dict]
    feedback: dict
    node_status: Annotated[dict[str, str], lambda left, right: {**(left or {}), **(right or {})}]
    errors: list[str]
    timing: Annotated[dict[str, float], lambda left, right: {**(left or {}), **(right or {})}]


ReportWriter = Callable[[str, dict], Awaitable[dict | str | None]]


def _risk_level(results: list[dict]) -> str:
    statuses = {str(item.get("status", "")).lower() for item in results}
    return "high" if "fail" in statuses or "error" in statuses else "medium" if "warning" in statuses else "low"


class CheckGraphOrchestrator:
    def __init__(
        self,
        llm: Any = None,
        checkpointer: Any = None,
        report_writer: ReportWriter | None = None,
        repair_runner: Any = None,
    ):
        self.llm = llm
        self.checkpointer = checkpointer
        self.report_writer = report_writer
        self.repair_runner = repair_runner
        self._graph = self._build_graph()

    def _config(self, run_id: str) -> dict:
        from core.settings import graph_runtime_config

        return graph_runtime_config(run_id)

    def _build_graph(self):
        graph = StateGraph(CheckGraphState)

        async def dispatch(state: CheckGraphState) -> dict:
            requested = state.get("check_ids")
            valid = {spec["check_id"] for spec in CHECK_REGISTRY}
            selected = [cid for cid in (requested or []) if cid in valid] if requested is not None else None
            return {"check_ids": selected, "node_status": {"check_dispatch": "done"}}

        async def execute(state: CheckGraphState) -> dict:
            started = time.monotonic()
            bid_text = state.get("bid_text", "")
            supplemental_evidence = ""
            # RAG 证据作为独立上下文传给材料型检查；不拼入最终正文，
            # 避免把企业资料误当成已签章的投标文件。
            try:
                from core.rag_engine.project_evidence import retrieve

                # ``None`` means the full registry, not an empty query list.
                # The previous ``or []`` silently disabled project-RAG
                # retrieval on full runs, so material checks ignored uploaded
                # evidence while focused rechecks appeared to work.
                requested = state.get("check_ids")
                if requested is None:
                    requested = [spec["check_id"] for spec in CHECK_REGISTRY]
                project_id = str(state.get("project_id") or "")
                # 按检查项分开检索，再按来源去重。一个泛查询会让资格索引、
                # 报价资料等高频文档挤掉检测报告或 CA 证据，导致“已上传但检查仍缺失”。
                queries = list(dict.fromkeys(str(item) for item in requested if str(item).strip()))
                evidence = []
                seen_sources: set[str] = set()
                for check_id in queries:
                    rows = await retrieve(project_id, f"项目补充资料 {check_id}", top_k=12)
                    for row in rows:
                        source = str((row.get("metadata") or {}).get("source") or row.get("id") or "")
                        if source and source in seen_sources:
                            continue
                        if source:
                            seen_sources.add(source)
                        evidence.append(row)
                evidence = evidence[:24]
                snippets = [
                    f"【RAG补充证据：{(item.get('metadata') or {}).get('source', '项目资料')}】\n"
                    f"{str(item.get('text') or '')[:1200]}"
                    for item in evidence
                    if str(item.get("text") or "").strip()
                ]
                supplemental_evidence = "\n\n".join(snippets)
            except Exception:  # noqa: BLE001 - RAG 不可用时保持原检查链路
                pass
            results = await run_all_checks(
                tender_text=state.get("tender_text", ""),
                bid_text=bid_text,
                llm=self.llm,
                project_id=state.get("project_id", ""),
                check_ids=state.get("check_ids"),
                extra_params={"supplemental_evidence": supplemental_evidence},
            )
            return {
                "results": results,
                "node_status": {"check_execute": "done"},
                "timing": {"check_execute_ms": round((time.monotonic() - started) * 1000, 2)},
            }

        async def report(state: CheckGraphState) -> dict:
            results = list(state.get("results") or [])
            from services.check.feedback_loop import extract_repair_queue

            # G-6 T1 按章节映射：确定性加载项目章节标题（有正文的章），供 finding→chapter 定位
            chapter_titles: dict[str, str] = {}
            try:
                from sqlalchemy import select

                from services.database import async_session
                from services.models import Chapter as ChapterRow

                async with async_session()() as db:
                    rows = (
                        await db.execute(
                            select(ChapterRow.id, ChapterRow.title).where(
                                ChapterRow.project_id == state.get("project_id", ""),
                                ChapterRow.content.isnot(None),
                                ChapterRow.content != "",
                            )
                        )
                    ).all()
                chapter_titles = {str(r.id): str(r.title or "") for r in rows}
            except Exception:  # noqa: BLE001
                chapter_titles = {}

            repair_queue = extract_repair_queue(results, chapter_titles=chapter_titles)
            # Worker I 任务1：确定性标注 fact 型缺料 finding（前端据此出"补料/复检"引导按钮）。
            # 与 services/check/missing_materials.py 同一关键词口径，无 LLM。
            from services.check.missing_materials import is_missing_material_text, is_non_current_material_matter

            missing_findings: list[dict] = []
            for item in results:
                if not isinstance(item, dict) or str(item.get("status", "")).lower() not in ("fail", "warning"):
                    continue
                data_obj = item.get("data") if isinstance(item.get("data"), dict) else {}
                for ref in data_obj.get("checks") or []:
                    if not isinstance(ref, dict):
                        continue
                    detail = str(ref.get("detail") or ref.get("reason") or "")
                    if is_missing_material_text(detail):
                        missing_findings.append(
                            {
                                "check_id": item.get("check_id", ""),
                                "check_name": item.get("check_name", ""),
                                "chapter_id": ref.get("chapter_id") or item.get("chapter_id") or "",
                                "detail": detail[:200],
                                "material_required": not is_non_current_material_matter(detail),
                            }
                        )
            report = {
                "check_results": results,
                "results": {item.get("check_id", "unknown"): item for item in results},
                "risk_level": _risk_level(results),
                "summary": {
                    "total": len(results),
                    "passed": sum(item.get("status") == "pass" for item in results),
                    "failed": sum(item.get("status") in ("fail", "error") for item in results),
                    "warning": sum(item.get("status") == "warning" for item in results),
                    "skipped": sum(item.get("status") == "skipped" for item in results),
                    # Worker I：缺料计数（补料引导闭环的数据源）
                    "missing_material_findings": len(missing_findings),
                },
                "repair_queue": repair_queue,
                "missing_material_findings": missing_findings[:200],
            }
            feedback = {"total": len(repair_queue), "fixed": 0, "recheck_pass_rate": 1.0, "tasks": []}
            if repair_queue and self.repair_runner is not None:
                feedback = await self.repair_runner(state, repair_queue)
                report["feedback"] = feedback

            # Repairs update Chapter rows, but the initial ``results`` list was
            # produced before those writes.  Re-run the selected checks once
            # against the current persisted chapters so the final report and
            # decision gate cannot retain stale findings from the pre-repair
            # snapshot.  This is intentionally a single post-repair pass and
            # never invokes the repair runner recursively.
            if repair_queue and feedback:
                try:
                    from core.agent_engine.master_graph import load_bid_text_if_missing

                    latest_bid_text = await load_bid_text_if_missing(
                        str(state.get("project_id") or ""), ""
                    )
                    post_repair_evidence = ""
                    project_id = str(state.get("project_id") or "")
                    if project_id:
                        from core.rag_engine.project_evidence import retrieve

                        evidence_rows = []
                        seen_sources: set[str] = set()
                        requested_checks = state.get("check_ids")
                        if requested_checks is None:
                            requested_checks = [spec["check_id"] for spec in CHECK_REGISTRY]
                        for check_id in requested_checks:
                            for row in await retrieve(
                                project_id, f"项目补充资料 {check_id}", top_k=12
                            ):
                                source = str(
                                    (row.get("metadata") or {}).get("source")
                                    or row.get("id")
                                    or ""
                                )
                                if source and source in seen_sources:
                                    continue
                                if source:
                                    seen_sources.add(source)
                                evidence_rows.append(row)
                        post_repair_evidence = "\n\n".join(
                            f"【RAG补充证据：{(row.get('metadata') or {}).get('source', '项目资料')}】\n"
                            f"{str(row.get('text') or '')[:1200]}"
                            for row in evidence_rows[:24]
                            if str(row.get("text") or "").strip()
                        )
                    refreshed = await run_all_checks(
                        tender_text=state.get("tender_text", ""),
                        bid_text=latest_bid_text or state.get("bid_text", ""),
                        llm=self.llm,
                        project_id=project_id,
                        check_ids=state.get("check_ids"),
                        extra_params={"supplemental_evidence": post_repair_evidence},
                    )
                    if refreshed:
                        results = refreshed
                        report["check_results"] = results
                        report["results"] = {
                            item.get("check_id", "unknown"): item for item in results
                        }
                        report["risk_level"] = _risk_level(results)
                        report["summary"].update(
                            {
                                "total": len(results),
                                "passed": sum(item.get("status") == "pass" for item in results),
                                "failed": sum(
                                    item.get("status") in ("fail", "error") for item in results
                                ),
                                "warning": sum(item.get("status") == "warning" for item in results),
                                "skipped": sum(item.get("status") == "skipped" for item in results),
                            }
                        )
                        # Rebuild material findings from the refreshed result
                        # set; otherwise the report would mix new statuses with
                        # old pre-repair missing-material entries.
                        missing_findings = []
                        for item in results:
                            if not isinstance(item, dict) or str(item.get("status", "")).lower() not in (
                                "fail",
                                "warning",
                            ):
                                continue
                            data_obj = item.get("data") if isinstance(item.get("data"), dict) else {}
                            for ref in data_obj.get("checks") or []:
                                if not isinstance(ref, dict):
                                    continue
                                detail = str(ref.get("detail") or ref.get("reason") or "")
                                if is_missing_material_text(detail):
                                    missing_findings.append(
                                        {
                                            "check_id": item.get("check_id", ""),
                                            "check_name": item.get("check_name", ""),
                                            "chapter_id": ref.get("chapter_id") or item.get("chapter_id") or "",
                                            "detail": detail[:200],
                                            "material_required": not is_non_current_material_matter(detail),
                                        }
                                    )
                except Exception:  # noqa: BLE001 - keep original report on degraded recheck
                    pass
            from services.check.missing_materials import build_action_summary, reconcile_missing_findings

            reconciled_missing = reconcile_missing_findings(missing_findings, feedback)
            # 只有当前阶段确实需要企业材料的 finding 才进入缺料清单；
            # 后续阶段/人工执行事项单独保留，不能污染“待补材料”数量。
            current_missing = [
                item for item in reconciled_missing if item.get("material_required", True) is not False
            ]
            workflow_findings = [
                item for item in reconciled_missing if item.get("material_required", True) is False
            ]
            report["missing_material_findings"] = current_missing[:200]
            report["summary"]["missing_material_findings"] = len(current_missing)
            report["workflow_findings"] = workflow_findings[:200]
            # Keep check-level problems, material findings and chapter repair
            # tasks as separate metrics. They answer different questions.
            report["action_summary"] = build_action_summary(results, reconciled_missing, feedback)
            # G-5：persist_report=False（upload-check 场景）跳过落库，报告仍进快照
            if self.report_writer and state.get("persist_report", True):
                persisted = await self.report_writer(state.get("project_id", ""), report)
                if persisted:
                    report["persisted"] = persisted
            return {
                "report": report,
                "repair_queue": repair_queue,
                "feedback": feedback,
                "node_status": {"check_report": "done"},
            }

        async def export(state: CheckGraphState) -> dict:
            from core.skill_engine.base import SkillContext
            from services.check.skills.check_report_export_skill import CheckReportExportSkill

            export_data = {
                item.get("check_id", "unknown"): (
                    {"success": False, "error": item.get("reason", "执行异常")}
                    if item.get("status") == "error"
                    else {"success": True, "data": item.get("data") or {"risk_level": item.get("status", "skipped")}}
                )
                for item in (state.get("results") or [])
            }
            outputs: dict[str, str] = {}
            for fmt in state.get("formats") or ["markdown", "html"]:
                result = await CheckReportExportSkill().safe_execute(
                    SkillContext(
                        project_id=state.get("project_id", ""),
                        db=None,
                        llm=None,
                        parameters={
                            "report_data": export_data,
                            "format": fmt,
                            "project_name": state.get("project_name", "未命名项目"),
                        },
                    )
                )
                if result.success:
                    outputs[fmt] = result.data.get("content", "")
            return {"exports": outputs, "node_status": {"check_export": "done"}}

        graph.add_node("check_dispatch", dispatch)
        graph.add_node("check_execute", execute)
        graph.add_node("check_report", report)
        graph.add_node("check_export", export)
        graph.add_edge(START, "check_dispatch")
        graph.add_edge("check_dispatch", "check_execute")
        graph.add_edge("check_execute", "check_report")
        graph.add_edge("check_report", "check_export")
        graph.add_edge("check_export", END)
        return graph.compile(checkpointer=self.checkpointer) if self.checkpointer is not None else graph.compile()

    async def run(self, run_id: str, run_input: dict) -> dict:
        return await self._graph.ainvoke({"run_id": run_id, **run_input}, self._config(run_id))

    async def resume(self, run_id: str) -> dict:
        return await self._graph.ainvoke(None, self._config(run_id))

    async def snapshot(self, run_id: str) -> dict:
        if self.checkpointer is None:
            return {}
        state = await self._graph.aget_state(self._config(run_id))
        return dict(state.values or {})


@dataclass
class CheckRunRecord:
    run_id: str
    project_id: str
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    snapshot: dict = field(default_factory=dict)
    error: str = ""


class CheckRunManager:
    def __init__(self, orchestrator: CheckGraphOrchestrator | None = None):
        self._orchestrator = orchestrator
        self._runs: dict[str, CheckRunRecord] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _ensure_orchestrator(self) -> CheckGraphOrchestrator:
        if self._orchestrator is None:
            from core.agent_engine.checkpoint import PGCheckpointSaver
            from services.database import async_session
            from services.llm_factory import get_llm_gateway

            async def persist(project_id: str, report: dict) -> dict:
                from services.models import CheckReport

                async with async_session()() as session:
                    row = CheckReport(
                        project_id=project_id,
                        type="full",
                        results=report.get("results") or {},
                        risk_level=report.get("risk_level", "low"),
                        summary=report.get("summary") or {},
                    )
                    session.add(row)
                    await session.commit()
                    return {"report_id": row.id, "type": row.type}

            async def repair_runner(state: dict, queue: list[dict]) -> dict:
                from services.check.repair_runner import production_repair_runner

                return await production_repair_runner(state, queue)

            self._orchestrator = CheckGraphOrchestrator(
                llm=get_llm_gateway(), checkpointer=PGCheckpointSaver(async_session()), report_writer=persist,
                # G-6 T1：生产修复回路接线（检查出问题→单章重写→硬门→复检）
                repair_runner=repair_runner,
            )
        return self._orchestrator

    def set_orchestrator(self, orchestrator: CheckGraphOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def create_run(self, payload: dict) -> CheckRunRecord:
        project_id = str(payload.get("project_id") or "")
        if not project_id:
            raise ValueError("project_id 必填")
        run_id = f"check_{uuid.uuid4().hex[:12]}"
        record = CheckRunRecord(run_id=run_id, project_id=project_id)
        self._runs[run_id] = record

        async def _run() -> None:
            try:
                record.snapshot = await self._ensure_orchestrator().run(run_id, payload)
                record.status = "completed" if not record.snapshot.get("errors") else "failed"
            except Exception as exc:  # noqa: BLE001
                record.status = "failed"
                record.error = str(exc)[:500]

        self._tasks[run_id] = asyncio.create_task(_run())
        return record

    async def wait_settled(self, run_id: str, timeout: float = 600) -> CheckRunRecord:
        record = self._runs[run_id]
        task = self._tasks.get(run_id)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return record

    async def resume(self, run_id: str) -> dict:
        record = self._runs.get(run_id)
        if record is None:
            snap = await self._ensure_orchestrator().snapshot(run_id)
            if not snap.get("project_id"):
                raise KeyError(run_id)
            record = CheckRunRecord(
                run_id=run_id,
                project_id=str(snap["project_id"]),
                status="running",
                snapshot=snap,
            )
            self._runs[run_id] = record
        record.snapshot = await self._ensure_orchestrator().resume(run_id)
        record.status = "completed"
        return record.snapshot

    def get(self, run_id: str, project_id: str | None = None) -> CheckRunRecord:
        record = self._runs.get(run_id)
        if record is None or (project_id and record.project_id != project_id):
            raise KeyError(run_id)
        return record


_manager: CheckRunManager | None = None


def get_check_run_manager() -> CheckRunManager:
    global _manager
    if _manager is None:
        _manager = CheckRunManager()
    return _manager


def set_check_run_manager(manager: CheckRunManager | None) -> None:
    global _manager
    _manager = manager


__all__ = [
    "CheckGraphState", "CheckGraphOrchestrator", "CheckRunManager", "CheckRunRecord",
    "get_check_run_manager", "set_check_run_manager",
]
