"""G-1 资格预审图运行服务层（图模式入口的 RunManager）。

与 services/graph_runtime/runner.py（主编排图 RunManager）平行的资格预审图运行管理：
- create_run：校验输入 → 后台任务跑资格预审图（提取→比对→HITL 审批门 interrupt）；
- decide：图模式人工审批（confirm/reject/mark_insufficient，语义复用旧状态机函数）；
- apply_timeout_policies：资格审批门人工专属，永不自动决策（只返回 wait_human）；
- 旧自研状态机与既有 API 一律不动（只读回归并存，G-4 收口）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.agent_engine.qualification_graph import QualificationGraphOrchestrator
from services.qualification.workflow import (
    DuplicateDecisionError,
    UnknownRequirementError,
    WorkflowNotFoundError,
    WorkflowStore,
    _coerce_decision,
)


@dataclass
class QualificationRunRecord:
    run_id: str
    project_id: str
    status: str = "running"  # running / waiting_human / completed / failed
    created_at: float = field(default_factory=time.time)
    error: str = ""
    snapshot: dict = field(default_factory=dict)


class QualificationGraphRunManager:
    """资格预审图运行注册表（内存）+ 持久 checkpointer（复用 PG JSONB saver）。"""

    def __init__(self, checkpointer: Any = None, orchestrator: QualificationGraphOrchestrator | None = None):
        self._checkpointer = checkpointer
        self._orchestrator = orchestrator
        self._runs: dict[str, QualificationRunRecord] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _ensure_orchestrator(self) -> QualificationGraphOrchestrator:
        if self._orchestrator is None:
            if self._checkpointer is None:
                from services.database import async_session, is_db_ready

                if is_db_ready():
                    from core.agent_engine.checkpoint import PGCheckpointSaver

                    self._checkpointer = PGCheckpointSaver(async_session())
                else:
                    # DB 不可用（轻量测试环境）：内存 checkpointer，interrupt/审批语义完整、
                    # 不跨事件循环持有连接（每个请求一个 loop 的 TestClient 场景必需）
                    from langgraph.checkpoint.memory import InMemorySaver

                    self._checkpointer = InMemorySaver()
            self._orchestrator = QualificationGraphOrchestrator(checkpointer=self._checkpointer)
        return self._orchestrator

    def set_orchestrator(self, orchestrator: QualificationGraphOrchestrator) -> None:
        """测试注入：替换编排器（内存 checkpointer / FakeLLM 场景）。"""
        self._orchestrator = orchestrator

    def reset(self) -> None:
        self._runs.clear()
        self._tasks.clear()
        self._orchestrator = None

    # ---- 运行生命周期 ----

    async def create_run(self, payload: dict) -> QualificationRunRecord:
        """启动一次资格预审图运行。payload 为图模式请求体 dict（router 已转 model_dump）。"""
        if not (payload.get("requirements") or payload.get("dimensions")):
            raise ValueError("requirements 与 dimensions 至少提供一个（图模式入口）")
        orchestrator = self._ensure_orchestrator()
        run_id = f"qrun_{uuid.uuid4().hex[:12]}"
        record = QualificationRunRecord(run_id=run_id, project_id=payload.get("project_id") or "")
        self._runs[run_id] = record

        async def _run() -> None:
            try:
                snap = await orchestrator.run_until_interrupt(run_id, dict(payload))
                record.snapshot = snap
                record.status = "waiting_human" if snap.get("pending_gate") else "completed"
            except Exception as e:  # noqa: BLE001
                record.status = "failed"
                record.error = str(e)[:500]

        self._tasks[run_id] = asyncio.create_task(_run())
        return record

    async def wait_settled(self, run_id: str, timeout: float = 120) -> QualificationRunRecord:
        """等待 run 到达挂起/终态（API 与测试用；本图无 LLM，通常瞬时完成）。"""
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError(run_id)
        task = self._tasks.get(run_id)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return record

    def get(self, run_id: str) -> QualificationRunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    def list_runs(self) -> list[QualificationRunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    # ---- 图模式人工审批（语义=旧状态机 approve_qualification_workflow）----

    def _prevalidate(self, run_id: str, decisions: list[dict]) -> None:
        """resume 前置校验（与旧状态机 approve 同口径：未知项/重复改判/数据无效提前拒绝）。"""
        wf = WorkflowStore.instance().get(run_id)
        if wf is None:
            raise WorkflowNotFoundError(f"Workflow '{run_id}' 不存在")
        parsed = [_coerce_decision(d) for d in decisions]
        item_ids = {item.requirement_id for item in wf.review_items}
        unknown = [d.requirement_id for d in parsed if d.requirement_id not in item_ids]
        if unknown:
            raise UnknownRequirementError("审批中包含未知的要求ID: " + ", ".join(unknown))
        existing = {rec["requirement_id"]: rec["decision"] for rec in wf.decisions}
        for d in parsed:
            if d.requirement_id in existing and existing[d.requirement_id] != d.decision:
                raise DuplicateDecisionError(
                    f"要求 '{d.requirement_id}' 已审批为 {existing[d.requirement_id]}，不能改为 {d.decision}"
                )

    async def decide(self, run_id: str, decisions: list[dict]) -> dict:
        record = self.get(run_id)
        if record.status != "waiting_human":
            raise ValueError(f"run {run_id} 当前状态 {record.status} 不在资格审批门")
        self._prevalidate(run_id, decisions)
        orchestrator = self._ensure_orchestrator()
        snap = await orchestrator.resume(run_id, decisions)
        record.snapshot = snap
        if snap.get("hitl_error") or snap.get("pending_gate"):
            record.status = "waiting_human"  # 多轮审批：仍有未决策项则继续等待人工
        else:
            record.status = "completed"
        return snap

    # ---- 铁律4：超时巡检（人工专属门，永不自动决策）----

    async def apply_timeout_policies(self) -> list[dict]:
        orchestrator = self._ensure_orchestrator()
        outcomes = []
        for record in self.list_runs():
            if record.status != "waiting_human":
                continue
            try:
                outcomes.append(await orchestrator.apply_gate_timeout(record.run_id))
            except Exception as e:  # noqa: BLE001
                outcomes.append({"run_id": record.run_id, "action": "error", "reason": str(e)})
        return outcomes


_manager: QualificationGraphRunManager | None = None


def get_qualification_run_manager() -> QualificationGraphRunManager:
    global _manager
    if _manager is None:
        _manager = QualificationGraphRunManager()
    return _manager


def set_qualification_run_manager(manager: QualificationGraphRunManager | None) -> None:
    """测试注入/重置。"""
    global _manager
    _manager = manager


__all__ = [
    "QualificationRunRecord",
    "QualificationGraphRunManager",
    "get_qualification_run_manager",
    "set_qualification_run_manager",
]
