"""G-4R 解读子图运行时：统一承载解读、评分矩阵和风险节点。"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph


class InterpretGraphState(TypedDict, total=False):
    run_id: str
    project_id: str
    tender_text: str
    mode: str
    scoring_data: dict
    node_status: Annotated[dict[str, str], lambda left, right: {**(left or {}), **(right or {})}]
    interpret_result: dict
    matrix_result: dict
    risk_result: dict
    finalized: bool


class InterpretGraphOrchestrator:
    def __init__(self, llm: Any = None, checkpointer: Any = None):
        self.llm = llm
        self.checkpointer = checkpointer
        self._graph = self._build_graph()

    def _config(self, run_id: str) -> dict:
        from core.settings import graph_runtime_config

        return graph_runtime_config(run_id)

    def _build_graph(self):
        from services.routers.route_skill_shims import (
            RiskAlertSkill,
            ScoringMatrixSkill,
            TenderInterpretSkill,
        )

        graph = StateGraph(InterpretGraphState)

        async def dispatch(state: dict) -> dict:
            # 旧工作流语义：每个端点只跑一个 Skill；mode 决定走哪条支路
            return {"node_status": {"interpret_dispatch": "done"}, "mode": state.get("mode") or "interpret"}

        async def react(state: dict) -> dict:
            result = await TenderInterpretSkill().safe_execute(
                __import__("core.skill_engine.base", fromlist=["SkillContext"]).SkillContext(
                    project_id=state.get("project_id", ""), db=None, llm=self.llm,
                    parameters={"document_text": state.get("tender_text", "")},
                )
            )
            return {
                "interpret_result": {
                    "success": result.success, "data": result.data,
                    "error": result.error, "warnings": result.warnings,
                },
                "node_status": {"interpret_react": "done"},
            }

        async def matrix(state: dict) -> dict:
            # scoring_data 优先取垫片传入的库内已有维度；无则回退本 run 的解读产物
            data = (state.get("interpret_result") or {}).get("data") or {}
            scoring = state.get("scoring_data") or data.get("dimensions", {}).get("scoring", {})
            result = await ScoringMatrixSkill().safe_execute(
                __import__("core.skill_engine.base", fromlist=["SkillContext"]).SkillContext(
                    project_id=state.get("project_id", ""), db=None, llm=self.llm,
                    parameters={"scoring_data": scoring},
                )
            )
            return {
                "matrix_result": {"success": result.success, "data": result.data, "error": result.error},
                "node_status": {"interpret_matrix": "done"},
            }

        async def risk(state: dict) -> dict:
            result = await RiskAlertSkill().safe_execute(
                __import__("core.skill_engine.base", fromlist=["SkillContext"]).SkillContext(
                    project_id=state.get("project_id", ""), db=None, llm=self.llm,
                    parameters={"tender_text": state.get("tender_text", "")[:6000]},
                )
            )
            return {
                "risk_result": {"success": result.success, "data": result.data, "error": result.error},
                "node_status": {"interpret_risk": "done"},
            }

        async def finalize(state: dict) -> dict:
            return {"node_status": {"interpret_finalize": "done"}, "finalized": True}

        def _route(state: dict) -> str:
            return {
                "interpret": "interpret_react",
                "matrix": "interpret_matrix",
                "risk": "interpret_risk",
            }.get(state.get("mode") or "interpret", "interpret_react")

        graph.add_node("interpret_dispatch", dispatch)
        graph.add_node("interpret_react", react)
        graph.add_node("interpret_matrix", matrix)
        graph.add_node("interpret_risk", risk)
        graph.add_node("interpret_finalize", finalize)
        graph.add_edge(START, "interpret_dispatch")
        graph.add_conditional_edges(
            "interpret_dispatch", _route,
            {
                "interpret_react": "interpret_react",
                "interpret_matrix": "interpret_matrix",
                "interpret_risk": "interpret_risk",
            },
        )
        graph.add_edge("interpret_react", "interpret_finalize")
        graph.add_edge("interpret_matrix", "interpret_finalize")
        graph.add_edge("interpret_risk", "interpret_finalize")
        graph.add_edge("interpret_finalize", END)
        return graph.compile(checkpointer=self.checkpointer) if self.checkpointer else graph.compile()

    async def run(self, run_id: str, payload: dict) -> dict:
        return await self._graph.ainvoke({"run_id": run_id, **payload}, self._config(run_id))

    async def snapshot(self, run_id: str) -> dict:
        if self.checkpointer is None:
            return {}
        state = await self._graph.aget_state(self._config(run_id))
        return dict(state.values or {})


@dataclass
class InterpretRunRecord:
    run_id: str
    project_id: str
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    snapshot: dict = field(default_factory=dict)
    error: str = ""


class InterpretRunManager:
    def __init__(self, orchestrator: InterpretGraphOrchestrator | None = None):
        self._orchestrator = orchestrator
        self._runs: dict[str, InterpretRunRecord] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _ensure_orchestrator(self) -> InterpretGraphOrchestrator:
        if self._orchestrator is None:
            from services.llm_factory import get_llm_gateway
            self._orchestrator = InterpretGraphOrchestrator(llm=get_llm_gateway())
        return self._orchestrator

    async def create_run(self, payload: dict) -> InterpretRunRecord:
        project_id = str(payload.get("project_id") or "")
        run_id = f"interpret_{uuid.uuid4().hex[:12]}"
        record = InterpretRunRecord(run_id=run_id, project_id=project_id)
        self._runs[run_id] = record

        async def _run() -> None:
            try:
                record.snapshot = await self._ensure_orchestrator().run(run_id, payload)
                record.status = "completed" if record.snapshot.get("finalized") else "failed"
            except Exception as exc:  # noqa: BLE001
                record.status = "failed"
                record.error = str(exc)[:500]

        self._tasks[run_id] = asyncio.create_task(_run())
        return record

    async def wait_settled(self, run_id: str, timeout: float = 360) -> InterpretRunRecord:
        record = self._runs[run_id]
        task = self._tasks.get(run_id)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return record

    def get(self, run_id: str) -> InterpretRunRecord:
        return self._runs[run_id]


_manager: InterpretRunManager | None = None


def get_interpret_run_manager() -> InterpretRunManager:
    global _manager
    if _manager is None:
        _manager = InterpretRunManager()
    return _manager


__all__ = ["InterpretGraphOrchestrator", "InterpretRunManager", "InterpretRunRecord", "get_interpret_run_manager"]
