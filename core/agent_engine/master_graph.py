"""G-6 T3 top-level graph: four existing business graphs as subgraph nodes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from core.agent_engine.decision import build_decision_package


class MasterGraphState(TypedDict, total=False):
    run_id: str
    project_id: str
    skip: list[str]
    outline_only: bool
    input: dict
    stage_results: Annotated[dict[str, dict], lambda left, right: {**(left or {}), **(right or {})}]
    node_status: Annotated[dict[str, str], lambda left, right: {**(left or {}), **(right or {})}]
    current_stage: str
    errors: list[str]
    decision_state: dict
    decision_package: dict
    pending_gate: str | None
    human_decision: dict
    final_level: str
    gate_namespace: str
    progress: dict
    stage_started_at: Annotated[dict[str, str], lambda left, right: {**(left or {}), **(right or {})}]
    selected_chapter_ids: list[str]
    generation_batches: list[list[str]]
    generation_batch_index: int
    qualification_refresh_index: int
    failed: bool


StageRunner = Callable[[str, dict], Awaitable[dict]]
STAGES = ("interpret", "qualification", "generate", "check")
GENERATION_BATCH_SIZE = 15

STAGE_PROGRESS = {
    "interpret": ("招标解读", "正在提取资格要求、评分项、关键时间和风险提示"),
    "qualification": ("资格核对", "正在把招标资格要求与企业证明材料逐条比对"),
    "outline": ("大纲生成", "正在根据招标结构、评分项和响应要求生成投标文件目录"),
    "generate": ("正文生成", "正在按已确认范围分批生成正文，每批最多 15 章"),
    "check": ("检查修复", "正在执行合规检查，并修复能够自动处理的问题"),
    "decision": ("投标决策", "正在汇总资格、检查、风险和缺料情况"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def map_check_to_decision_state(master_state: dict) -> dict:
    """Explicitly adapt structured check output into P-D1 decision inputs."""
    stages = master_state.get("stage_results") or {}
    check = stages.get("check") or {}
    report = check.get("report") if isinstance(check.get("report"), dict) else check
    raw_results = report.get("check_results")
    if not isinstance(raw_results, list):
        result_map = report.get("results") if isinstance(report.get("results"), dict) else {}
        raw_results = []
        for check_id, item in result_map.items():
            if isinstance(item, dict):
                raw_results.append({"check_id": check_id, **item})
    risk_summary = {
        "level": str(report.get("risk_level") or "low"),
        "summary": report.get("summary") if isinstance(report.get("summary"), dict) else {},
        "feedback": report.get("feedback") if isinstance(report.get("feedback"), dict) else {},
    }
    interpretation = (stages.get("interpret") or {}).get("interpretation")
    qualification = stages.get("qualification") or {}
    return {
        "project_id": master_state.get("project_id", ""),
        "rule_results": raw_results,
        "risk_summary": risk_summary,
        "interpretation": interpretation if isinstance(interpretation, dict) else {},
        "expert_results": {"qualification": qualification},
        "generation_result": stages.get("generate") or {},
    }


class BidMasterGraphOrchestrator:
    """Compose already-built graphs without duplicating their node code.

    ``stage_runners`` is injectable for tests and for the service adapters. Each
    runner receives the same run id and a stage-specific payload.
    """

    def __init__(
        self,
        stage_runners: dict[str, StageRunner],
        checkpointer: Any = None,
        enable_decision: bool = False,
        stage_resumers: dict[str, Callable[[str, Any], Awaitable[dict]]] | None = None,
    ):
        self.stage_runners = stage_runners
        self.checkpointer = checkpointer
        self.enable_decision = enable_decision
        self.stage_resumers = stage_resumers or {}
        self._graph = self._build_graph()

    def _config(self, run_id: str) -> dict:
        from core.settings import graph_runtime_config

        return graph_runtime_config(run_id)

    def _build_graph(self):
        graph = StateGraph(MasterGraphState)
        def mark_stage(stage_name: str):
            def marker(_state: MasterGraphState) -> dict:
                label, message = STAGE_PROGRESS[stage_name]
                started_at = _now_iso()
                result = {
                    "node_status": {stage_name: "running"},
                    "current_stage": stage_name,
                    "stage_started_at": {stage_name: started_at},
                    "progress": {
                        "stage": stage_name,
                        "stage_label": label,
                        "message": message,
                        "started_at": started_at,
                    },
                }
                # 复检开始时清掉上一轮报告，避免前端在新检查完成前
                # 把旧的 CAUTION/NO_BID 当成当前结论。
                if stage_name == "check":
                    result["stage_results"] = {"check": {}}
                    result["decision_package"] = None
                    result["final_level"] = ""
                return result

            return marker

        def stage_payload(state: MasterGraphState) -> dict:
            payload = dict(state.get("input") or {})
            payload.update(
                {
                    "project_id": state.get("project_id", ""),
                    "outline_only": state.get("outline_only", False),
                    "stage_results": state.get("stage_results", {}),
                }
            )
            return payload

        def standard_stage(stage_name: str):
            async def run_stage(state: MasterGraphState) -> dict:
                if stage_name in set(state.get("skip") or []):
                    return {
                        "stage_results": {stage_name: {"skipped": True}},
                        "node_status": {stage_name: "skipped"},
                        "current_stage": f"{stage_name}_complete",
                    }
                result = await self.stage_runners[stage_name](state["run_id"], stage_payload(state))
                return {
                    "stage_results": {stage_name: result},
                    "node_status": {stage_name: "done" if not result.get("error") else "error"},
                    "current_stage": f"{stage_name}_complete",
                    "errors": [str(result["error"])] if result.get("error") else [],
                }

            return run_stage

        if not self.enable_decision:
            previous = START
            for stage in STAGES:
                start_node = f"{stage}_start"
                graph.add_node(start_node, mark_stage(stage))
                graph.add_node(stage, standard_stage(stage))
                graph.add_edge(previous, start_node)
                graph.add_edge(start_node, stage)
                previous = stage
            graph.add_edge(previous, END)
        else:
            graph.add_node("interpret_start", mark_stage("interpret"))
            graph.add_node("interpret", standard_stage("interpret"))
            graph.add_node("qualification_start", mark_stage("qualification"))
            graph.add_node("qualification", standard_stage("qualification"))

            def route_after_qualification(state: MasterGraphState) -> str:
                result = (state.get("stage_results") or {}).get("qualification") or {}
                return "qualification_hitl_gate" if result.get("pending_gate") else "outline_start"

            async def qualification_gate(state: MasterGraphState) -> dict:
                decision = interrupt(
                    {
                        "namespace": "qualification",
                        "gate_id": "qualification_hitl_gate",
                    "review_items": (
                        (state.get("stage_results") or {}).get("qualification") or {}
                    ).get("review_items", []),
                    }
                )
                if isinstance(decision, dict) and decision.get("action") == "refresh":
                    refresh_index = int(state.get("qualification_refresh_index") or 0) + 1
                    payload = stage_payload(state)
                    payload["qualification_refresh_index"] = refresh_index
                    result = await self.stage_runners["qualification"](state["run_id"], payload)
                    return {
                        "stage_results": {"qualification": result},
                        "qualification_refresh_index": refresh_index,
                        "node_status": {
                            "qualification_hitl_gate": "waiting" if result.get("pending_gate") else "done"
                        },
                        "current_stage": "qualification_refreshed",
                    }
                resumer = self.stage_resumers.get("qualification")
                if resumer is None:
                    raise RuntimeError("qualification resumer 未配置")
                decisions = decision.get("decisions", decision) if isinstance(decision, dict) else decision
                qualification_result = (state.get("stage_results") or {}).get("qualification") or {}
                result = await resumer(
                    state["run_id"],
                    {
                        "workflow_id": qualification_result.get("workflow_id"),
                        "decisions": decisions,
                    },
                )
                return {
                    "stage_results": {"qualification": result},
                    "node_status": {
                        "qualification_hitl_gate": (
                            "done" if not result.get("pending_gate") else "waiting"
                        )
                    },
                    "current_stage": "qualification_resumed",
                }

            def route_after_qualification_gate(state: MasterGraphState) -> str:
                result = (state.get("stage_results") or {}).get("qualification") or {}
                return "qualification_hitl_gate" if result.get("pending_gate") else "outline_start"

            async def run_outline(state: MasterGraphState) -> dict:
                if "generate" in set(state.get("skip") or []):
                    return {
                        "stage_results": {"outline": {"skipped": True, "chapters_plan": []}},
                        "node_status": {"outline": "skipped"},
                        "current_stage": "outline_complete",
                    }
                payload = stage_payload(state)
                payload.update(
                    {
                        "outline_only": True,
                        "run_outline": payload.get("run_outline", True),
                        "chapter_ids": [],
                        "generation_phase": "outline",
                    }
                )
                result = await self.stage_runners["generate"](state["run_id"], payload)
                plan = list(result.get("chapters_plan") or [])
                failed = bool(result.get("error") or result.get("errors") or not plan)
                return {
                    "stage_results": {"outline": result},
                    "node_status": {"outline": "error" if failed else "done"},
                    "current_stage": "outline_failed" if failed else "outline_complete",
                    "errors": list(result.get("errors") or ([str(result["error"])] if result.get("error") else [])),
                    "failed": failed,
                }

            def route_after_outline(state: MasterGraphState) -> str:
                return "failed_finalize" if state.get("failed") else "scope_hitl_gate"

            def scope_gate(state: MasterGraphState) -> dict:
                outline = (state.get("stage_results") or {}).get("outline") or {}
                chapters = list(outline.get("chapters_plan") or [])
                available_ids = [str(item.get("id")) for item in chapters if item.get("id")]
                requested = list((state.get("input") or {}).get("chapter_ids") or [])
                default_ids = [chapter_id for chapter_id in requested if chapter_id in available_ids] or available_ids
                selection = interrupt(
                    {
                        "namespace": "scope",
                        "gate_id": "scope_hitl_gate",
                        "title": "选择本次要生成的章节",
                        "chapters": chapters,
                        "default_chapter_ids": default_ids,
                        "batch_size": GENERATION_BATCH_SIZE,
                    }
                )
                chosen = selection.get("chapter_ids") if isinstance(selection, dict) else None
                chosen_set = {str(item) for item in (chosen if isinstance(chosen, list) else default_ids)}
                selected = [chapter_id for chapter_id in available_ids if chapter_id in chosen_set]
                if not selected:
                    return {
                        "node_status": {"scope": "error"},
                        "current_stage": "scope_failed",
                        "errors": ["至少需要选择一个章节才能继续生成"],
                        "failed": True,
                    }
                batches = [
                    selected[index : index + GENERATION_BATCH_SIZE]
                    for index in range(0, len(selected), GENERATION_BATCH_SIZE)
                ]
                return {
                    "selected_chapter_ids": selected,
                    "generation_batches": batches,
                    "generation_batch_index": 0,
                    "stage_results": {
                        "generate": {
                            "chapters_plan": chapters,
                            "selected_chapter_ids": selected,
                            "batches": [],
                            "chapters": [],
                        }
                    },
                    "node_status": {"scope": "done"},
                    "current_stage": "scope_complete",
                }

            def route_after_scope(state: MasterGraphState) -> str:
                return "failed_finalize" if state.get("failed") else "generate_start"

            async def run_generation_batch(state: MasterGraphState) -> dict:
                batches = list(state.get("generation_batches") or [])
                batch_index = int(state.get("generation_batch_index") or 0)
                if batch_index >= len(batches):
                    return {"node_status": {"generate": "done"}, "current_stage": "generate_complete"}
                batch = list(batches[batch_index])
                payload = stage_payload(state)
                payload.update(
                    {
                        "outline_only": False,
                        "run_outline": False,
                        "chapter_ids": batch,
                        "generation_phase": "batch",
                        "generation_batch_index": batch_index,
                    }
                )
                result = await self.stage_runners["generate"](state["run_id"], payload)
                previous = dict((state.get("stage_results") or {}).get("generate") or {})
                generated = list(previous.get("chapters") or []) + list(result.get("chapters") or [])
                batch_records = list(previous.get("batches") or [])
                batch_records.append(
                    {
                        "index": batch_index + 1,
                        "total": len(batches),
                        "chapter_ids": batch,
                        "result": result,
                    }
                )
                next_index = batch_index + 1
                failed_chapters = [
                    item
                    for item in (result.get("chapters_all") or [])
                    if isinstance(item, dict) and item.get("status") == "failed"
                ]
                generated_payload = [item for item in (result.get("chapters") or []) if isinstance(item, dict)]
                failed = bool(
                    result.get("error")
                    or result.get("errors")
                    or ("finalized" in result and not result.get("finalized"))
                    or failed_chapters
                    or len(generated_payload) < len(batch)
                )
                selected_total = len(state.get("selected_chapter_ids") or [])
                completed_count = min(sum(len(item) for item in batches[:next_index]), selected_total)
                generate_status = "error" if failed else "done" if next_index >= len(batches) else "running"
                current_stage = (
                    "generate_failed"
                    if failed
                    else "generate_complete"
                    if next_index >= len(batches)
                    else "generate"
                )
                return {
                    "stage_results": {
                        "generate": {
                            **previous,
                            "batches": batch_records,
                            "chapters": generated,
                            "latest": result,
                            "completed_chapters": completed_count,
                            "total_chapters": len(state.get("selected_chapter_ids") or []),
                        }
                    },
                    "generation_batch_index": next_index,
                    "node_status": {"generate": generate_status},
                    "current_stage": "generate_failed" if failed else current_stage,
                    "progress": {
                        "stage": "generate",
                        "stage_label": "正文生成",
                        "message": (
                            f"已完成第 {next_index}/{len(batches)} 批，"
                            f"累计 {completed_count}/{selected_total} 章"
                        ),
                        "batch_index": next_index,
                        "batch_total": len(batches),
                        "completed_items": completed_count,
                        "total_items": len(state.get("selected_chapter_ids") or []),
                        "started_at": (state.get("stage_started_at") or {}).get("generate"),
                    },
                    "errors": list(result.get("errors") or ([str(result["error"])] if result.get("error") else [])),
                    "failed": failed,
                }

            def route_after_generation(state: MasterGraphState) -> str:
                if state.get("failed"):
                    return "failed_finalize"
                if int(state.get("generation_batch_index") or 0) < len(state.get("generation_batches") or []):
                    return "generate_batch"
                return "check_start"

            async def decision_input(state: MasterGraphState) -> dict:
                mapped = map_check_to_decision_state(dict(state))
                package = build_decision_package(
                    mapped["rule_results"], mapped["expert_results"], mapped["risk_summary"]
                )
                return {
                    "decision_state": mapped,
                    "decision_package": package,
                    "node_status": {"rule_gate": "done", "decision_package": "done"},
                    "current_stage": "decision_ready",
                    "progress": {
                        "stage": "decision",
                        "stage_label": "投标决策",
                        "message": "系统已汇总全部结果，等待人工确认最终投标建议",
                        "started_at": _now_iso(),
                    },
                }

            async def decision_gate(state: MasterGraphState) -> dict:
                decision = interrupt(
                    {
                        "namespace": "decision",
                        "gate_id": "decision_hitl_gate",
                        "decision_package": state.get("decision_package") or {},
                    }
                )
                if isinstance(decision, dict) and decision.get("action") == "recheck":
                    selected_checks = decision.get("check_ids")
                    next_input = dict(state.get("input") or {})
                    # None means full check; an explicit list limits the next
                    # pass to the requested checks while preserving the same
                    # top-level run/checkpoint.
                    next_input["check_ids"] = (
                        [str(item) for item in selected_checks if str(item)]
                        if isinstance(selected_checks, list) and selected_checks
                        else None
                    )
                    return {
                        "input": next_input,
                        "human_decision": dict(decision),
                        "pending_gate": None,
                        "current_stage": "recheck_requested",
                        "progress": {
                            "stage": "check",
                            "stage_label": "重新检查与修复",
                            "message": "已记录复检范围，正在重新执行检查并修复可自动处理的问题",
                            "started_at": _now_iso(),
                        },
                    }
                level = str(decision.get("level") or (state.get("decision_package") or {}).get("level") or "")
                return {
                    "human_decision": dict(decision),
                    "pending_gate": None,
                    "final_level": level,
                    "node_status": {"decision_hitl_gate": "done"},
                }

            def route_after_decision(state: MasterGraphState) -> str:
                decision = state.get("human_decision") or {}
                return "check_start" if decision.get("action") == "recheck" else "finalize"

            def finalize(state: MasterGraphState) -> dict:
                return {
                    "current_stage": "finalized",
                    "node_status": {"finalize": "done"},
                    "pending_gate": None,
                    "progress": {
                        "stage": "finalize",
                        "stage_label": "已完成",
                        "message": "正文、检查结果和决策记录均已就绪，可以复核并导出",
                        "started_at": _now_iso(),
                    },
                }

            def failed_finalize(state: MasterGraphState) -> dict:
                return {
                    "current_stage": "failed",
                    "failed": True,
                    "node_status": {"finalize": "error"},
                    "pending_gate": None,
                    "progress": {
                        "stage": "failed",
                        "stage_label": "流程中断",
                        "message": (state.get("errors") or ["流程执行失败，请查看错误详情"])[-1],
                        "started_at": _now_iso(),
                    },
                }

            graph.add_node("qualification_hitl_gate", qualification_gate)
            graph.add_node("outline_start", mark_stage("outline"))
            graph.add_node("outline", run_outline)
            graph.add_node("scope_hitl_gate", scope_gate)
            graph.add_node("generate_start", mark_stage("generate"))
            graph.add_node("generate_batch", run_generation_batch)
            graph.add_node("check_start", mark_stage("check"))
            graph.add_node("check", standard_stage("check"))
            graph.add_node("rule_gate", decision_input)
            graph.add_node("decision_hitl_gate", decision_gate)
            graph.add_node("finalize", finalize)
            graph.add_node("failed_finalize", failed_finalize)
            graph.add_edge(START, "interpret_start")
            graph.add_edge("interpret_start", "interpret")
            graph.add_edge("interpret", "qualification_start")
            graph.add_edge("qualification_start", "qualification")
            graph.add_conditional_edges(
                "qualification",
                route_after_qualification,
                {"qualification_hitl_gate": "qualification_hitl_gate", "outline_start": "outline_start"},
            )
            graph.add_conditional_edges(
                "qualification_hitl_gate",
                route_after_qualification_gate,
                {"qualification_hitl_gate": "qualification_hitl_gate", "outline_start": "outline_start"},
            )
            graph.add_edge("outline_start", "outline")
            graph.add_conditional_edges(
                "outline",
                route_after_outline,
                {"scope_hitl_gate": "scope_hitl_gate", "failed_finalize": "failed_finalize"},
            )
            graph.add_conditional_edges(
                "scope_hitl_gate",
                route_after_scope,
                {"generate_start": "generate_start", "failed_finalize": "failed_finalize"},
            )
            graph.add_edge("generate_start", "generate_batch")
            graph.add_conditional_edges(
                "generate_batch",
                route_after_generation,
                {
                    "generate_batch": "generate_batch",
                    "check_start": "check_start",
                    "failed_finalize": "failed_finalize",
                },
            )
            graph.add_edge("check_start", "check")
            graph.add_edge("check", "rule_gate")
            graph.add_edge("rule_gate", "decision_hitl_gate")
            graph.add_conditional_edges(
                "decision_hitl_gate",
                route_after_decision,
                {"check_start": "check_start", "finalize": "finalize"},
            )
            graph.add_edge("finalize", END)
            graph.add_edge("failed_finalize", END)
        return graph.compile(checkpointer=self.checkpointer)

    async def run(self, run_id: str, run_input: dict) -> dict:
        # run_stage 节点从 state["input"] 取业务载荷；tender_text/analysis_dimensions/
        # memory_query/run_outline/check_ids/skip 等必须进 input（平铺顶层会全部丢失，
        # 曾致 grun_e2555da8e6cd runaway：payload 空 → run_outline 默认 True 全量正文）。
        merged = dict(run_input or {})
        await self._graph.ainvoke(
            {
                "run_id": run_id,
                "input": merged,
                "project_id": str(merged.get("project_id") or ""),
                "skip": list(merged.get("skip") or []),
                "outline_only": bool(merged.get("outline_only", False)),
                "node_status": {"upload": "done", "parse": "done"} if self.enable_decision else {},
                "current_stage": "queued",
                "progress": {
                    "stage": "queued",
                    "stage_label": "准备开始",
                    "message": "招标文件已上传并解析，正在进入 AI 解读",
                    "started_at": _now_iso(),
                },
            },
            self._config(run_id),
        )
        return await self.snapshot(run_id)

    async def run_until_interrupt(self, run_id: str, run_input: dict) -> dict:
        return await self.run(run_id, run_input)

    async def resume(self, run_id: str, decision: dict | None = None) -> dict:
        await self._graph.ainvoke(Command(resume=decision or {"action": "approve"}), self._config(run_id))
        return await self.snapshot(run_id)

    async def apply_gate_timeout(self, run_id: str, threshold: float) -> dict:
        snap = await self.snapshot(run_id)
        if (
            snap.get("pending_gate_namespace") == "decision"
            and snap.get("decision_package", {}).get("level") == "CAUTION"
        ):
            return {"run_id": run_id, "action": "approve", "applied": True, "reason": "CAUTION timeout"}
        return {
            "run_id": run_id,
            "action": "wait",
            "applied": False,
            "reason": "NO_BID/qualification gate requires human",
        }

    def cost_report(self, run_id: str) -> dict:
        return {"run_id": run_id, "nodes": {}, "total_llm_calls": 0, "total_tokens": 0, "total_duration_ms": 0}

    async def snapshot(self, run_id: str) -> dict:
        state = await self._graph.aget_state(self._config(run_id))
        values = dict(state.values or {})
        next_nodes = list(state.next or [])
        interrupt_namespaces = {
            str(getattr(interrupt_item, "value", {}).get("namespace"))
            for task in (state.tasks or [])
            for interrupt_item in (getattr(task, "interrupts", None) or ())
            if isinstance(getattr(interrupt_item, "value", None), dict)
        }
        # 终态后不得再报挂起门：state 里残留的 gate_namespace（门触发时写入）会让
        # completed run 仍显示 pending_decision（grun_233cef2ec675 实证），必须先判终态。
        pending_namespace = (
            None
            if values.get("current_stage") in {"finalized", "failed"}
            else "qualification"
            if "qualification" in interrupt_namespaces or "qualification_hitl_gate" in next_nodes
            else "scope"
            if "scope" in interrupt_namespaces or "scope_hitl_gate" in next_nodes
            else "decision"
            if "decision" in interrupt_namespaces or "decision_hitl_gate" in next_nodes
            else None
        )
        progress = dict(values.get("progress") or {})
        # 上传/解析是总图运行的前置事实，不是后续 AI 节点。旧 checkpoint
        # 在业务节点写入时曾丢失这两个状态，导致历史运行前两步显示为灰色。
        # 真实服务创建 run 前已校验招标文件存在且已解析，因此对启用决策的
        # 总图统一补回这两个状态；不改变节点执行语义，只修正可视化快照。
        node_status = dict(values.get("node_status") or {})
        if self.enable_decision:
            node_status.setdefault("upload", "done")
            node_status.setdefault("parse", "done")
        if pending_namespace == "qualification":
            progress = {
                "stage": "qualification",
                "stage_label": "系统核对完成，等待你确认",
                "message": "请逐条核对招标原文和企业证明材料；缺少证据时可在当前页面补充上传",
            }
        elif pending_namespace == "scope":
            progress = {
                "stage": "scope",
                "stage_label": "投标大纲已完成",
                "message": "请选择本次要生成的章节，确认后系统将按每批最多 15 章继续",
            }
        elif pending_namespace == "decision":
            progress = {
                "stage": "decision",
                "stage_label": "检查与修复已完成",
                "message": "请查看投标建议、风险和缺料情况，并确认最终结论",
            }
        started_at = progress.get("started_at")
        if started_at:
            try:
                progress["elapsed_seconds"] = max(
                    0,
                    int((datetime.now(timezone.utc) - datetime.fromisoformat(str(started_at))).total_seconds()),
                )
            except (TypeError, ValueError):
                pass
        return {
            "run_id": run_id,
            "project_id": values.get("project_id", ""),
            "stage_results": values.get("stage_results", {}),
            "node_status": node_status,
            "current_stage": values.get("current_stage", ""),
            "errors": values.get("errors", []),
            "decision_package": values.get("decision_package"),
            "pending_gate": (
                "qualification_hitl_gate"
                if pending_namespace == "qualification"
                else "scope_hitl_gate"
                if pending_namespace == "scope"
                else "decision_hitl_gate"
                if pending_namespace == "decision"
                else values.get("pending_gate")
            ),
            "pending_gate_namespace": pending_namespace,
            "progress": progress,
            "selected_chapter_ids": values.get("selected_chapter_ids", []),
            "generation_batches": values.get("generation_batches", []),
            "generation_batch_index": values.get("generation_batch_index", 0),
            "human_decision": values.get("human_decision"),
            "final_level": values.get("final_level", ""),
            "failed": bool(values.get("failed")),
            "next_nodes": next_nodes,
            "completed": (
                values.get("current_stage") == "finalized" and not values.get("failed")
                if self.enable_decision
                else all(
                    (values.get("node_status") or {}).get(stage) in {"done", "skipped"}
                    for stage in STAGES
                )
            ),
        }


async def load_bid_text_if_missing(project_id: str, bid_text: str) -> str:
    """G7-3 修复：总图 check 段的 bid_text 在 run 创建时快照（新建项目此时无正文），
    generate 之后复用旧空值 → 22 项检查全 skipped、修复闭环无法发生。此处按项目
    DB 章节现载（与 services/graph_runtime/runner.py _load_project_texts 同口径）。"""
    if (bid_text or "").strip() or not project_id:
        return bid_text or ""
    try:
        from sqlalchemy import select

        from services.database import async_session
        from services.models import Chapter, Document

        async with async_session()() as db:
            chapters = ((await db.execute(select(Chapter).where(Chapter.project_id == project_id))).scalars().all())

            # Reconcile persisted chapters against the project's current evidence
            # before checking.  Generation is intentionally append-friendly, but
            # checks must never see two competing versions of a hard fact.  The
            # harmonizer is topic-aware and uses newest project uploads first, so
            # this remains generic for arbitrary projects and document types.
            try:
                ref_rows = (
                    await db.execute(
                        select(Document)
                        .where(Document.project_id == project_id, Document.type.in_(["reference", "bid"]))
                        .order_by(Document.created_at.desc())
                    )
                ).scalars().all()
                from core.agent_engine.evidence_gate import build_ledger
                from core.agent_engine.fact_harmonizer import harmonize_content
                from core.rag_engine.project_evidence import _is_internal_placeholder, _is_internal_source

                evidence_docs = []
                for row in ref_rows:
                    text = str(row.parsed_content or "").strip()
                    source = str(row.original_name or "")
                    if not text or _is_internal_placeholder(text) or _is_internal_source(source):
                        continue
                    evidence_docs.append(
                        {
                            "text": text,
                            "metadata": {
                                "document_id": str(row.id),
                                "source": source,
                                "collection": f"kb_proj_{str(project_id).replace('-', '')[:32]}",
                                "chunk_id": f"project_doc_{row.id}",
                            },
                        }
                    )
                ledger = build_ledger(evidence_docs)
                changed = False
                for chapter in chapters:
                    if not chapter.content:
                        continue
                    harmonized, _meta = harmonize_content(chapter.content, chapter.title or "", ledger)
                    if harmonized != chapter.content:
                        chapter.content = harmonized
                        chapter.word_count = len(harmonized)
                        changed = True
                if changed:
                    await db.commit()
            except Exception:  # noqa: BLE001 - checking must remain available if reconciliation is unavailable
                # Evidence reconciliation is an enhancement, not a prerequisite
                # for rebuilding the persisted bid text.  Keep the chapter rows
                # already loaded even when an offline/test session does not
                # expose Document fields or transaction helpers.
                rollback = getattr(db, "rollback", None)
                if rollback is not None:
                    try:
                        await rollback()
                    except Exception:  # noqa: BLE001
                        pass
        # 检查模型通常有上下文窗口限制。把决定资格/报价/有效期/签章的章节
        # 放在前面，避免总目录或旧模板占满窗口后造成“正文不存在”的假阴性。
        priority_words = (
            "资格审查资料", "营业执照及基本资质", "财务状况报告", "税收", "社会保障",
            "无重大违法", "投标函", "有效期", "报价汇总", "分项报价", "价格构成",
            "保证金", "授权委托", "法定代表人", "电子投标", "签章", "联合体",
        )
        def chapter_key(ch: Any) -> tuple[int, int, str]:
            title = str(ch.title or "")
            hits = sum(1 for word in priority_words if word in title)
            # 命中越多越靠前；同等命中时保留原章节顺序。
            return (-hits, int(getattr(ch, "sort_order", 0) or 0), title)
        chapters.sort(key=chapter_key)
        raw = "\n\n".join(f"## {ch.title}\n{ch.content or ''}" for ch in chapters if ch.content)
        # 检查应基于可交付正文；生成期引用锚点/知识库元数据不属于投标文件，
        # 统一在进入检查前净化，避免 eBid/签章检查把内部痕迹当成提交内容。
        from core.agent_engine.export_sanitizer import sanitize_export_text

        cleaned, _ = sanitize_export_text(raw)
        return cleaned
    except Exception:  # noqa: BLE001 — 现载失败保持原值，检查按输入不足显式 skipped
        return bid_text or ""


def build_production_stage_runners(llm: Any, checkpointer: Any) -> tuple[dict[str, StageRunner], dict[str, Callable]]:
    """Build adapters around the four existing compiled business graphs."""
    from core.agent_engine.generate_graph import GenerationGraphOrchestrator
    from core.agent_engine.qualification_graph import QualificationGraphOrchestrator
    from services.check.graph_runtime import CheckGraphOrchestrator
    from services.interpret.graph_runtime import InterpretGraphOrchestrator

    interpret = InterpretGraphOrchestrator(llm=llm, checkpointer=checkpointer)
    qualification = QualificationGraphOrchestrator(checkpointer=checkpointer)
    generate = GenerationGraphOrchestrator(llm=llm, checkpointer=checkpointer)
    check = CheckGraphOrchestrator(llm=llm, checkpointer=checkpointer)

    async def check_repair_runner(state: dict, queue: list[dict]) -> dict:
        from services.check.repair_runner import production_repair_runner

        return await production_repair_runner(state, queue, llm)

    check.repair_runner = check_repair_runner

    async def run_interpret(run_id: str, payload: dict) -> dict:
        memory_context = ""
        if payload.get("memory_query"):
            from core.agent_framework.memory import LongTermMemory

            memory_context = await LongTermMemory().recall_context(payload["memory_query"])
        if payload.get("analysis_dimensions"):
            return {
                "interpretation": {
                    "success": True,
                    "data": {"dimensions": payload["analysis_dimensions"]},
                    "memory_context": memory_context,
                },
                "memory_context": memory_context,
                "source": "persisted_analysis",
            }
        result = await interpret.run(
            f"{run_id}:interpret",
            {"project_id": payload.get("project_id", ""), "tender_text": payload.get("tender_text", "")},
        )
        return {
            "interpretation": result.get("interpret_result") or {},
            "memory_context": memory_context,
            **result,
        }

    async def run_qualification(run_id: str, payload: dict) -> dict:
        from services.qualification.analysis_adapter import adapt_analysis
        from services.qualification.evidence_loader import load_qualification_credentials
        from services.qualification.matcher import match_qualifications
        from services.qualification.models import Credential, Requirement

        previous = payload.get("stage_results", {}).get("interpret", {})
        data = (previous.get("interpretation") or {}).get("data") or {}
        dimensions = payload.get("dimensions") or data.get("dimensions") or data
        raw_requirements = payload.get("requirements") or data.get("qualification_requirements") or []
        adapter_warnings: list[str] = []
        unresolved_count = 0
        requirements: list[Requirement] = []
        for raw in raw_requirements if isinstance(raw_requirements, list) else []:
            try:
                requirements.append(Requirement.model_validate(raw))
            except Exception:  # noqa: BLE001 - 非结构化列表交由 dimensions 适配器处理
                requirements = []
                break
        if not requirements and isinstance(dimensions, dict):
            adapted = adapt_analysis(dimensions)
            requirements = list(adapted.requirements)
            adapter_warnings = list(adapted.warnings)
            unresolved_count = len(adapted.unresolved_items)
        credentials, evidence_warnings = await load_qualification_credentials(
            str(payload.get("project_id") or ""),
            requirements,
            explicit=payload.get("credentials") or [],
        )
        # In the production master graph, adapter warnings are informational
        # when every structured hard requirement is met. Keep the standalone
        # qualification APIs' stricter HITL semantics unchanged, while
        # preventing a fully evidenced project from stopping on harmless
        # parsing warnings.
        try:
            pre_report = match_qualifications(
                requirements,
                [Credential.model_validate(item) for item in credentials],
            )
            blocking_review = any(item.status != "met" or item.warnings for item in pre_report.results)
        except Exception:  # noqa: BLE001
            blocking_review = True
        force_review = (unresolved_count > 0 or bool(payload.get("force_review"))) and blocking_review
        refresh_index = int(payload.get("qualification_refresh_index") or 0)
        child_run_id = (
            f"{run_id}:qualification"
            if not refresh_index
            else f"{run_id}:qualification:refresh:{refresh_index}"
        )
        result = await qualification.run_until_interrupt(
            child_run_id,
            {
                "project_id": payload.get("project_id", ""),
                "requirements": [item.model_dump() for item in requirements],
                "dimensions": {},
                "credentials": credentials,
                "adapter_warnings": adapter_warnings + evidence_warnings,
                "unresolved_count": unresolved_count,
                "force_review": force_review,
            },
        )
        result["credential_count"] = len(credentials)
        result["evidence_warnings"] = evidence_warnings
        return result

    async def resume_qualification(run_id: str, decisions: Any) -> dict:
        payload = decisions if isinstance(decisions, dict) else {"decisions": decisions}
        child_run_id = str(payload.get("workflow_id") or f"{run_id}:qualification")
        return await qualification.resume(child_run_id, list(payload.get("decisions") or []))

    async def run_generate(run_id: str, payload: dict) -> dict:
        phase = str(payload.get("generation_phase") or "full")
        if phase == "batch":
            child_run_id = f"{run_id}:generate:batch:{int(payload.get('generation_batch_index') or 0) + 1}"
        elif phase == "outline":
            child_run_id = f"{run_id}:generate:outline"
        else:
            child_run_id = f"{run_id}:generate"
        return await generate.run(
            child_run_id,
            {
                "project_id": payload.get("project_id", ""),
                "outline_only": bool(payload.get("outline_only", False)),
                "run_outline": bool(payload.get("run_outline", True)),
                "chapter_modes": payload.get("chapter_modes") or {},
                "chapter_ids": payload.get("chapter_ids") or [],
                "memory_context": payload.get("memory_context", ""),
            },
        )

    async def run_check(run_id: str, payload: dict) -> dict:
        # 正文生成阶段会把最新内容写入 Chapter；项目里原先上传的
        # ``type=bid`` 文档可能仍是旧版本，不能让检查阶段优先读到旧快照。
        # 先强制按项目现载章节读取，确实没有章节正文时再回退输入快照。
        project_id = payload.get("project_id", "")
        latest_bid_text = await load_bid_text_if_missing(project_id, "")
        result = await check.run(
            f"{run_id}:check",
            {
                "project_id": project_id,
                "tender_text": payload.get("tender_text", ""),
                "bid_text": latest_bid_text or payload.get("bid_text", ""),
                "formats": [],
                "check_ids": payload.get("check_ids"),
            },
        )
        return result

    return (
        {"interpret": run_interpret, "qualification": run_qualification, "generate": run_generate, "check": run_check},
        {"qualification": resume_qualification},
    )


__all__ = [
    "BidMasterGraphOrchestrator",
    "MasterGraphState",
    "STAGES",
    "map_check_to_decision_state",
    "build_production_stage_runners",
]
