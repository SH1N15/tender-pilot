"""P-D1 图运行服务层。

RunManager：管理图运行的注册表（内存）+ 持久 checkpointer（PG JSONB saver）。
- create_run: 读项目标书/投标文本（与 services/routers/check.py 相同的只读取数方式），
  后台任务启动全图跑到 HITL 决策门；
- decide: approve / override(必带理由) → resume 图执行；
- 超时策略（铁律4）：所有最终建议均须人工确认，阈值仅用于监控告警；
- 改判理由写回图状态（override_reason）并可经 API 查询；同时写本地记录日志
  （规则审核/飞轮深度对接留待后续，本期只留记录接口）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.agent_engine.checkpoint import PGCheckpointSaver
from core.agent_engine.iron_rules import DEFAULT_DECISION_TIMEOUT_SECONDS
from core.agent_engine.master_graph import BidMasterGraphOrchestrator, build_production_stage_runners

logger = logging.getLogger(__name__)

# 铁律4：超时阈值可配置（环境变量，测试可用秒级小值）
DEFAULT_TIMEOUT = float(os.getenv("GRAPH_DECISION_TIMEOUT_SECONDS", str(DEFAULT_DECISION_TIMEOUT_SECONDS)))

OVERRIDE_LOG = os.getenv("GRAPH_OVERRIDE_LOG", ".dev/graph_override_log.jsonl")


@dataclass
class RunRecord:
    run_id: str
    project_id: str
    status: str = "starting"  # starting/running/pending_decision/finalized/failed
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    decided_by: str = ""
    error: str = ""
    snapshot: dict = field(default_factory=dict)
    run_options: dict = field(default_factory=dict, repr=False)


class RunManager:
    def __init__(self, llm: Any = None, checkpointer: Any = None, timeout_seconds: float | None = None):
        self._llm = llm
        self._checkpointer = checkpointer
        self._orchestrator: BidMasterGraphOrchestrator | None = None
        self._runs: dict[str, RunRecord] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT
        self._lock = asyncio.Lock()

    # ---- 装配（惰性，避免 import 期连 DB）----

    def _ensure_orchestrator(self) -> BidMasterGraphOrchestrator:
        if self._orchestrator is None:
            if self._llm is None:
                from services.llm_factory import get_llm_gateway

                self._llm = get_llm_gateway()
            if self._checkpointer is None:
                from services.database import async_session

                self._checkpointer = PGCheckpointSaver(async_session())
            runners, resumers = build_production_stage_runners(self._llm, self._checkpointer)
            self._orchestrator = BidMasterGraphOrchestrator(
                stage_runners=runners,
                stage_resumers=resumers,
                checkpointer=self._checkpointer,
                enable_decision=True,
            )
        return self._orchestrator

    def set_orchestrator(self, orchestrator: Any) -> None:
        """测试注入：替换编排器（FakeLLM/内存 checkpointer）。"""
        self._orchestrator = orchestrator

    def reset(self) -> None:
        self._runs.clear()
        self._tasks.clear()
        self._orchestrator = None

    # ---- 读取项目文本（只读，同 check.py 口径）----

    async def _load_project_texts(self, project_id: str) -> tuple[str, str, dict]:
        from sqlalchemy import select

        from services.database import async_session
        from services.models import Analysis, Chapter, Document, Outline, Project

        async with async_session()() as db:
            project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            tender_text = ""
            if project and project.tender_doc_id:
                doc = (
                    await db.execute(select(Document).where(Document.id == project.tender_doc_id))
                ).scalar_one_or_none()
                tender_text = doc.parsed_content or "" if doc else ""
            bid_docs = (
                await db.execute(
                    select(Document).where(Document.project_id == project_id, Document.type == "bid")
                )
            ).scalars().first()
            bid_text = bid_docs.parsed_content or "" if bid_docs else ""
            if not bid_text:
                chapters = (await db.execute(select(Chapter).where(Chapter.project_id == project_id))).scalars().all()
                bid_text = "\n\n".join(f"## {ch.title}\n{ch.content or ''}" for ch in chapters if ch.content)
            analysis = (
                await db.execute(select(Analysis).where(Analysis.project_id == project_id))
            ).scalar_one_or_none()
            outline = (
                await db.execute(select(Outline).where(Outline.project_id == project_id))
            ).scalar_one_or_none()
        return tender_text, bid_text, {
            "analysis_dimensions": (analysis.dimensions if analysis else {}) or {},
            "has_existing_outline": bool(outline and outline.tree),
        }

    # ---- 运行生命周期 ----

    async def create_run(self, project_id: str, run_options: dict | None = None) -> RunRecord:
        orchestrator = self._ensure_orchestrator()
        run_id = f"grun_{uuid.uuid4().hex[:12]}"
        record = RunRecord(
            run_id=run_id,
            project_id=project_id,
            status="running",
            run_options=dict(run_options or {}),
            snapshot={
                "run_id": run_id,
                "project_id": project_id,
                "node_status": {"upload": "done", "parse": "done"},
                "current_stage": "queued",
                "pending_gate": None,
                "pending_gate_namespace": None,
                "progress": {
                    "stage": "queued",
                    "stage_label": "准备开始",
                    "message": "招标文件已上传并解析，正在进入 AI 解读",
                },
            },
        )
        self._runs[run_id] = record

        async def _run() -> None:
            try:
                tender_text, bid_text, context = await self._load_project_texts(project_id)
                snap = await orchestrator.run_until_interrupt(
                    run_id,
                    {
                        "project_id": project_id,
                        "tender_text": tender_text,
                        "bid_text": bid_text,
                        **context,
                        **getattr(record, "run_options", {}),
                    },
                )
                record.snapshot = snap
                record.status = self._status_from_snapshot(snap)
            except Exception as e:  # noqa: BLE001
                record.status = "failed"
                record.error = str(e)[:500]
                logger.exception("graph run %s failed", run_id)

        self._tasks[run_id] = asyncio.create_task(_run())
        return record

    async def wait_settled(self, run_id: str, timeout: float = 600) -> RunRecord:
        """等待 run 到达挂起/终态（API 与测试用）。"""
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

    def get(self, run_id: str) -> RunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    async def get_or_reattach(self, run_id: str) -> RunRecord:
        """get 的可恢复版：重启后注册表为空，按 checkpoint 快照重挂（方案 A 附加要求 4：kill→重启→resume）。

        checkpoint 无该线程（或空状态）时抛 KeyError，与原语义一致。
        """
        record = self._runs.get(run_id)
        if record is not None:
            await self._refresh_snapshot(record)
            if record.status == "running" and not self._has_live_task(run_id):
                self._resume_orphaned(record)
            return record
        snap = await self._ensure_orchestrator().snapshot(run_id)
        if not (snap.get("current_stage") or snap.get("stage_results")):
            raise KeyError(run_id)
        status = self._status_from_snapshot(snap)
        record = RunRecord(run_id=run_id, project_id=str(snap.get("project_id") or ""), status=status, snapshot=snap)
        record.created_at = self._snapshot_created_at(snap)
        self._runs[run_id] = record
        if record.status == "running":
            self._resume_orphaned(record)
        return record

    def _has_live_task(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task is not None and not task.done())

    def _resume_orphaned(self, record: RunRecord) -> None:
        """为服务重启后遗留的 running checkpoint 重新挂载异步任务。"""
        if self._has_live_task(record.run_id):
            return
        orchestrator = self._ensure_orchestrator()

        async def _resume() -> None:
            try:
                snap = await orchestrator.resume(record.run_id)
                record.snapshot = snap
                record.status = self._status_from_snapshot(snap)
            except Exception as exc:  # noqa: BLE001
                record.status = "failed"
                record.error = str(exc)[:500]
                record.snapshot = {
                    **(record.snapshot or {}),
                    "failed": True,
                    "current_stage": "failed",
                    "errors": [record.error],
                    "progress": {
                        "stage": "failed",
                        "stage_label": "流程中断",
                        "message": "服务重启后尝试续跑失败：" + record.error,
                    },
                }
                logger.exception("orphaned graph run %s resume failed", record.run_id)

        self._tasks[record.run_id] = asyncio.create_task(_resume())

    @staticmethod
    def _snapshot_created_at(snapshot: dict) -> float:
        """从 checkpoint 恢复运行创建时间，避免重启后历史运行冒充最新。"""
        ts = snapshot.get("created_at") or snapshot.get("_checkpoint_created_at") or snapshot.get("ts")
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return 0.0

    @staticmethod
    def _status_from_snapshot(snapshot: dict) -> str:
        if snapshot.get("failed") or snapshot.get("current_stage") == "failed":
            return "failed"
        # A completed/finalized snapshot wins over stale gate fields left by
        # the interrupt checkpoint.  This matters after restart/reattach and
        # keeps a resumed run from being reported as pending forever.
        if snapshot.get("completed") or snapshot.get("current_stage") in {
            "finalized",
            "complete",
            "done",
            "exported",
        }:
            return "finalized"
        if snapshot.get("pending_gate") or snapshot.get("pending_gate_namespace"):
            return "pending_decision"
        # 资格子图在恢复数据无效时可能把自身标成 waiting_human，但父图已
        # 写出 qualification_resumed 且清空 pending_gate，导致运行永久显示
        # running、页面也没有资格确认控件。此时仍然是可恢复的资格人工门。
        qualification = (snapshot.get("stage_results") or {}).get("qualification") or {}
        if (
            str(snapshot.get("current_stage") or "")
            in {"qualification", "qualification_resumed", "qualification_refreshed"}
            and (
                str(qualification.get("workflow_status") or "") == "waiting_human"
                or qualification.get("hitl_error")
                or qualification.get("review_items")
            )
        ):
            return "pending_decision"
        next_nodes = snapshot.get("next_nodes") or []
        if any("hitl_gate" in str(node) for node in next_nodes):
            return "pending_decision"
        current_stage = str(snapshot.get("current_stage") or "")
        if current_stage in {"qualification", "qualification_complete", "outline_complete", "check_complete"}:
            return "pending_decision"
        return "running"

    async def _refresh_snapshot(self, record: RunRecord) -> None:
        """运行中直接读持久 checkpoint，让长节点的阶段标记和批次进度可见。"""
        if record.status not in {"running", "pending_decision"}:
            return
        try:
            snapshot = await self._ensure_orchestrator().snapshot(record.run_id)
        except Exception:  # noqa: BLE001 - 读取进度失败不应打断运行详情
            logger.debug("graph run %s checkpoint refresh failed", record.run_id, exc_info=True)
            return
        if not (snapshot.get("current_stage") or snapshot.get("stage_results")):
            return
        record.snapshot = snapshot
        task = self._tasks.get(record.run_id)
        if task is None or task.done():
            record.status = self._status_from_snapshot(snapshot)

    def list_runs(self) -> list[RunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    async def list_runs_with_history(self, limit: int = 100) -> list[RunRecord]:
        """Merge live records with persisted snapshots so restarts keep history visible."""
        records: dict[str, RunRecord] = dict(self._runs)
        try:
            orchestrator = self._ensure_orchestrator()
            saver = getattr(orchestrator, "checkpointer", None) or self._checkpointer
            loader = getattr(saver, "alist_latest_snapshots", None)
            if loader is not None:
                for run_id, snapshot in await loader(limit):
                    if not run_id.startswith("grun_"):
                        continue
                    # saver 返回的是原始 channel_values；pending_gate、final_level
                    # 等字段由主编排器 snapshot() 统一计算，列表必须使用归一化快照，
                    # 否则重启后所有挂起运行都会伪装成 running。
                    raw_created_at = self._snapshot_created_at(snapshot)
                    try:
                        normalized = await orchestrator.snapshot(run_id)
                        if isinstance(normalized, dict) and normalized.get("current_stage"):
                            if raw_created_at > 0:
                                normalized["_checkpoint_created_at"] = snapshot.get("_checkpoint_created_at")
                            snapshot = normalized
                    except Exception:  # noqa: BLE001 - 单个历史线程异常不影响列表
                        pass
                    if run_id in records:
                        await self._refresh_snapshot(records[run_id])
                        if records[run_id].created_at <= 0:
                            records[run_id].created_at = raw_created_at or self._snapshot_created_at(
                                records[run_id].snapshot
                            )
                        continue
                    if not (snapshot.get("current_stage") or snapshot.get("stage_results")):
                        continue
                    created_at = self._snapshot_created_at(snapshot)
                    records[run_id] = RunRecord(
                        run_id=run_id,
                        project_id=str(snapshot.get("project_id") or ""),
                        status=self._status_from_snapshot(snapshot),
                        snapshot=snapshot,
                        created_at=created_at,
                    )
        except Exception:  # noqa: BLE001 - history is best effort; live runs remain available
            logger.debug("graph run history load failed", exc_info=True)
        return sorted(records.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    # ---- 决策（铁律5：RBAC 上层已把关；override 必带理由）----

    async def decide(
        self,
        run_id: str,
        action: str,
        reason: str,
        level: str | None,
        user: str,
        namespace: str = "decision",
        decisions: list[dict] | None = None,
        check_ids: list[str] | None = None,
    ) -> dict:
        record = await self.get_or_reattach(run_id)
        if record.status != "pending_decision":
            raise ValueError(f"run {run_id} 当前状态 {record.status} 不在决策门")
        if action == "override":
            if not (reason or "").strip():
                raise ValueError("override 必须携带理由（铁律5）")
            self._log_override(record, level, reason, user)
        orchestrator = self._ensure_orchestrator()
        decision: dict[str, Any] = {"action": action, "reason": reason, "decided_by": user}
        if namespace == "qualification":
            decision = {"action": "refresh"} if action == "refresh" else {"decisions": list(decisions or [])}
        elif namespace == "scope":
            decision = {
                "action": "confirm_scope",
                "chapter_ids": [str(item) for item in (decisions or []) if str(item)],
                "decided_by": user,
            }
        elif action == "recheck":
            decision = {
                "action": "recheck",
                "check_ids": [str(item) for item in (check_ids or []) if str(item)],
                "decided_by": user,
            }
        if level:
            decision["level"] = level
        record.status = "running"
        record.snapshot = {
            **(record.snapshot or {}),
            "pending_gate": None,
            "pending_gate_namespace": None,
            "override_reason": (
                reason.strip() if action == "override" else (record.snapshot or {}).get("override_reason")
            ),
            "human_decision": decision,
            "progress": {
                "stage": "resuming",
                "stage_label": "继续运行",
                "message": "人工确认已记录，系统正在进入下一阶段",
            },
        }
        record.decided_at = time.time()
        record.decided_by = user

        async def _resume() -> None:
            try:
                snap = await orchestrator.resume(run_id, decision)
                record.snapshot = snap
                record.status = self._status_from_snapshot(snap)
            except Exception as e:  # noqa: BLE001
                record.status = "failed"
                record.error = str(e)[:500]
                record.snapshot = {
                    **(record.snapshot or {}),
                    "failed": True,
                    "current_stage": "failed",
                    "errors": [record.error],
                    "progress": {
                        "stage": "failed",
                        "stage_label": "流程中断",
                        "message": record.error,
                    },
                }
                logger.exception("graph run %s resume failed", run_id)

        self._tasks[run_id] = asyncio.create_task(_resume())
        return record.snapshot

    def _log_override(self, record: RunRecord, level: str | None, reason: str, user: str) -> None:
        """铁律5：改判理由记录接口（本期写 JSONL 日志；规则审核/飞轮后续对接）。"""
        try:
            os.makedirs(os.path.dirname(OVERRIDE_LOG) or ".", exist_ok=True)
            entry = {
                "run_id": record.run_id,
                "project_id": record.project_id,
                "level": level,
                "reason": reason,
                "user": user,
                "at": datetime.now().isoformat(),
            }
            with open(OVERRIDE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("改判日志写入失败", exc_info=True)

    # ---- 铁律4：超时巡检 ----

    async def apply_timeout_policies(self) -> list[dict]:
        """对所有挂在决策门的 run 应用门型超时策略。返回每个 run 的处理结果。"""
        orchestrator = self._ensure_orchestrator()
        outcomes = []
        for record in self.list_runs():
            if record.status != "pending_decision":
                continue
            try:
                outcome = await orchestrator.apply_gate_timeout(record.run_id, self.timeout_seconds)
            except Exception as e:  # noqa: BLE001
                outcome = {"action": "error", "reason": str(e)}
            if outcome.get("action") == "approve" and outcome.get("applied"):
                record.status = "finalized"
                record.decided_at = time.time()
                record.decided_by = "system(auto-timeout)"
                record.snapshot = await orchestrator.snapshot(record.run_id)
            outcomes.append({"run_id": record.run_id, **outcome})
        return outcomes

    def cost(self, run_id: str) -> dict:
        self.get(run_id)
        return self._ensure_orchestrator().cost_report(run_id)


_manager: RunManager | None = None


# ---- P-F 阶段 A 新增采集项：图 HITL 决策聚合统计（纯函数，确定性可单测）----


def summarize_checkpoint_decisions(rows: list[tuple[str, Any]]) -> dict:
    """从 (thread_id, 终态 checkpoint 原始值) 行聚合 HITL 决策统计（只读、无副作用）。

    checkpoint 载荷解码前可为 dict 或 JSON 字符串；解码失败/缺字段的线程计入 malformed。
    批准率/改判率分母 = decided（approve+approve_auto+override），与 KPI 快照口径一致。
    """
    saver = PGCheckpointSaver(None)
    out = {
        "threads_total": 0,
        "malformed": 0,
        "decided": {"approve": 0, "approve_auto": 0, "override": 0, "none": 0},
        "final_levels": {},
        "override_reason_logged": 0,
        "pending_gate": 0,
        "grounding": {"total": 0, "passed": 0, "rejected": 0},
    }
    for thread_id, raw in rows:
        out["threads_total"] += 1
        try:
            cp = saver._load(raw)
            v = (cp or {}).get("channel_values") or {}
        except Exception:  # noqa: BLE001
            out["malformed"] += 1
            continue
        hd = v.get("human_decision") or {}
        action = str(hd.get("action", "")) if isinstance(hd, dict) else ""
        if v.get("current_stage") == "finalized":
            if action == "override":
                out["decided"]["override"] += 1
            elif hd.get("auto"):
                out["decided"]["approve_auto"] += 1
            elif action == "approve":
                out["decided"]["approve"] += 1
            else:
                out["decided"]["none"] += 1
        elif v.get("decision_package") is not None and not action:
            out["pending_gate"] += 1
        level = str(v.get("final_level") or "")
        if level:
            out["final_levels"][level] = out["final_levels"].get(level, 0) + 1
        if action == "override" and str(v.get("override_reason") or "").strip():
            out["override_reason_logged"] += 1
        eg = (v.get("evidence_grounding") or {}).get("stats") or {}
        for key in out["grounding"]:
            try:
                out["grounding"][key] += int(eg.get(key, 0) or 0)
            except (TypeError, ValueError):
                pass
    decided_total = (
        out["decided"]["approve"] + out["decided"]["approve_auto"] + out["decided"]["override"]
    )
    out["decided_total"] = decided_total
    approve_total = out["decided"]["approve"] + out["decided"]["approve_auto"]
    out["approve_rate"] = round(approve_total / decided_total, 4) if decided_total else 0.0
    out["override_rate"] = round(out["decided"]["override"] / decided_total, 4) if decided_total else 0.0
    return out


def get_run_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager


def set_run_manager(manager: RunManager | None) -> None:
    """测试注入/重置。"""
    global _manager
    _manager = manager
