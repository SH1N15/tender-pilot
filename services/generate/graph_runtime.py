"""G-2 章节生成图运行服务层（图模式入口的 RunManager）。

与 services/graph_runtime/runner.py（主编排图 RunManager）及
services/qualification/graph_runtime.py（G-1 资格预审 RunManager）同模式：
- create_run：校验输入 → 后台任务跑章节生成图（大纲→逐章正文→Grounding 硬门→落库）；
- resume：kill 后从最近 checkpoint 续写（ainvoke(None, thread_id)，已完成章不重写）；
- 无 HITL 门（章节生成无人工决策点），status 取值 running/completed/failed；
- 旧直调端点（/api/generate/*）一律不动（G-4 收口）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GenerationRunRecord:
    run_id: str
    project_id: str
    status: str = "running"  # running / completed / failed
    created_at: float = field(default_factory=time.time)
    error: str = ""
    snapshot: dict = field(default_factory=dict)


class GenerationRunManager:
    """章节生成图运行注册表（内存）+ 持久 checkpointer（复用 PG JSONB saver）。"""

    def __init__(self, llm: Any = None, checkpointer: Any = None, orchestrator: Any = None):
        self._llm = llm
        self._checkpointer = checkpointer
        self._orchestrator = orchestrator
        self._runs: dict[str, GenerationRunRecord] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _ensure_orchestrator(self):
        if self._orchestrator is None:
            from core.agent_engine.generate_graph import GenerationGraphOrchestrator

            if self._llm is None:
                from services.llm_factory import get_llm_gateway

                self._llm = get_llm_gateway()
            if self._checkpointer is None:
                from core.agent_engine.checkpoint import PGCheckpointSaver
                from services.database import async_session

                self._checkpointer = PGCheckpointSaver(async_session())
            self._orchestrator = GenerationGraphOrchestrator(llm=self._llm, checkpointer=self._checkpointer)
        return self._orchestrator

    def set_orchestrator(self, orchestrator: Any) -> None:
        """测试注入：替换编排器（FakeLLM/内存 checkpointer 场景）。"""
        self._orchestrator = orchestrator

    def reset(self) -> None:
        self._runs.clear()
        self._tasks.clear()
        self._orchestrator = None

    # ---- 运行生命周期 ----

    async def create_run(self, payload: dict) -> GenerationRunRecord:
        """启动一次章节生成图运行。payload 见路由 GraphRunCreate。"""
        project_id = str(payload.get("project_id") or "")
        if not project_id:
            raise ValueError("project_id 必填（图模式入口）")
        orchestrator = self._ensure_orchestrator()
        run_id = f"gen_{uuid.uuid4().hex[:12]}"
        record = GenerationRunRecord(run_id=run_id, project_id=project_id)
        self._runs[run_id] = record

        run_input = {
            "project_id": project_id,
            "outline_mode": str(payload.get("outline_mode") or "aligned"),
            "run_outline": bool(payload.get("run_outline", True)),
            "outline_only": bool(payload.get("outline_only", False)),
            "chapter_modes": dict(payload.get("chapter_modes") or {}),
            "chapter_ids": list(payload.get("chapter_ids") or []),
        }

        async def _run() -> None:
            try:
                snap = await orchestrator.run(run_id, run_input)
                record.snapshot = snap
                errors = snap.get("errors") or []
                generated_ok = [c for c in (snap.get("chapters") or []) if c.get("status") == "generated"]
                outline_ok = bool((snap.get("outline_result") or {}).get("success"))
                if snap.get("finalized") and (generated_ok or snap.get("chapters_plan") or outline_ok):
                    record.status = "completed" if not errors else "failed"
                    if errors:
                        record.error = "; ".join(str(e) for e in errors)[:500]
                else:
                    record.status = "failed"
                    record.error = "; ".join(str(e) for e in errors)[:500] or "图运行未到终态"
            except Exception as e:  # noqa: BLE001
                record.status = "failed"
                record.error = str(e)[:500]
                logger.exception("generation graph run %s failed", run_id)

        self._tasks[run_id] = asyncio.create_task(_run())
        return record

    async def wait_settled(self, run_id: str, timeout: float = 600) -> GenerationRunRecord:
        """等待 run 到达终态（API 与测试用）。"""
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

    def get(self, run_id: str, project_id: str | None = None) -> GenerationRunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError(run_id)
        if project_id and record.project_id != project_id:
            raise KeyError(run_id)
        return record

    def list_runs(self, project_id: str | None = None) -> list[GenerationRunRecord]:
        runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        if project_id:
            runs = [r for r in runs if r.project_id == project_id]
        return runs

    # ---- kill 后续写（无 HITL，resume=checkpoint 续跑）----

    async def _rebuild_record_from_checkpoint(self, run_id: str) -> GenerationRunRecord | None:
        """进程重启后由 checkpointer 重建 run 记录（run_id 与 checkpoint 同 thread_id）。"""
        orchestrator = self._ensure_orchestrator()
        try:
            snap = await orchestrator.snapshot(run_id)
        except Exception:  # noqa: BLE001
            return None
        if not snap.get("project_id"):
            return None
        if not (snap.get("chapters_plan") or snap.get("chapters_all") or snap.get("current_stage")):
            return None
        record = GenerationRunRecord(run_id=run_id, project_id=str(snap["project_id"]), status="running", snapshot=snap)
        self._runs[run_id] = record
        return record

    async def resume(self, run_id: str, project_id: str | None = None) -> dict:
        record: GenerationRunRecord | None
        try:
            record = self.get(run_id, project_id)
        except KeyError:
            # kill 重启后内存注册表清空：由 checkpoint 重建 run 记录（thread_id=run_id）
            record = await self._rebuild_record_from_checkpoint(run_id)
            if record is None or (project_id and record.project_id != project_id):
                raise KeyError(run_id)
        if record.status == "completed":
            return record.snapshot  # 幂等：已完成 run 无需续写
        orchestrator = self._ensure_orchestrator()
        record.status = "running"

        async def _resume() -> None:
            try:
                snap = await orchestrator.resume(run_id)
                record.snapshot = snap
                generated_ok = [c for c in (snap.get("chapters") or []) if c.get("status") == "generated"]
                if snap.get("finalized") and generated_ok:
                    record.status = "completed"
                else:
                    record.status = "failed"
                    record.error = "; ".join(str(e) for e in (snap.get("errors") or []))[:500] or "续写未到终态"
            except Exception as e:  # noqa: BLE001
                record.status = "failed"
                record.error = str(e)[:500]
                logger.exception("generation graph resume %s failed", run_id)

        self._tasks[run_id] = asyncio.create_task(_resume())
        return record.snapshot

    async def live_snapshot(self, run_id: str, project_id: str | None = None) -> dict:
        """运行中实时快照（直接读 checkpointer 最新 checkpoint；轮询用）。"""
        self.get(run_id, project_id)
        try:
            return await self._ensure_orchestrator().snapshot(run_id)
        except Exception:  # noqa: BLE001  # 读不到就返回空（前端继续轮询）
            return {}

    def snapshot_payload(self, record: GenerationRunRecord, snap: dict | None = None) -> dict:
        """API 响应体：run 状态 + 章节数字（耗时/锚点/ledger/门通过率）。"""
        snap = snap if snap is not None else (record.snapshot or {})
        chapters = snap.get("chapters") or []
        return {
            "run_id": record.run_id,
            "project_id": record.project_id,
            "status": record.status,
            "error": record.error,
            "snapshot": {
                "current_stage": snap.get("current_stage", ""),
                "node_status": snap.get("node_status", {}),
                "next_nodes": snap.get("next_nodes", []),
                "outline_result": snap.get("outline_result"),
                "chapters_plan": snap.get("chapters_plan", []),
                "chapters": chapters,
                "chapters_all": snap.get("chapters_all", []),
                "grounding": snap.get("grounding", {}),
                "timing": snap.get("timing", {}),
                "errors": snap.get("errors", []),
                "warnings": snap.get("warnings", []),
                "finalized": snap.get("finalized", False),
            },
        }


_manager: GenerationRunManager | None = None


def get_generation_run_manager() -> GenerationRunManager:
    global _manager
    if _manager is None:
        _manager = GenerationRunManager()
    return _manager


def set_generation_run_manager(manager: GenerationRunManager | None) -> None:
    """测试注入/重置。"""
    global _manager
    _manager = manager


__all__ = [
    "GenerationRunRecord",
    "GenerationRunManager",
    "get_generation_run_manager",
    "set_generation_run_manager",
]
