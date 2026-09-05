"""资格预审数据飞轮：本地 append-only JSONL TraceStore + 指标。

定位：只做「收集 -> 脱敏 -> 评测数据」，不会自动学习、不会自动改规则、不调用 LLM、
不依赖外部服务。数据来源是真实项目运行 Trace 与 HITL 人工决策。

隐私边界（严格白名单）：
- 事件只允许写入文档约定的聚合字段（run / approval 各自白名单）；
- 嵌套字段做结构/类型收敛：summary 只保留 total/met/unmet/insufficient 数值，
  decision_counts 只保留 confirm/reject/mark_insufficient 数值；
- 未知字段、未知嵌套、未知事件类型一律删除；
- 不写入完整原始文档、credentials、source_text、evidence_refs、审批人姓名/备注、证据路径；
- project_id 只以不可逆 SHA-256 摘要 project_ref 保存；workflow 关联只保留由 workflow_id
  不可逆派生的 trace_id，不保存原始 workflow_id；
- TraceStore.append 在落盘前即调用白名单清洗，降低磁盘敏感数据残留风险；
  读取/导出再次清洗兜底。

健壮性：Trace 写入失败绝不导致主匹配/审批流程失败，只降级为 workflow/API warning。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.settings import get_settings

MATCHER_VERSION = "1.0.0"
ADAPTER_VERSION = "1.0.0"

TRACE_WRITE_FAILED_WARNING = "资格预审 Trace 记录写入失败（不影响本次结果）"

ENTRYPOINTS = ("manual", "from_analysis", "from_project")

# 事件允许的标量字段（白名单）
_COMMON_SCALAR_FIELDS: dict[str, tuple[type, ...]] = {
    "event_id": (str,),
    "event_type": (str,),
    "occurred_at": (str,),
    "trace_id": (str,),
    "project_ref": (str, type(None)),
    "entrypoint": (str, type(None)),
    "matcher_version": (str, type(None)),
    "adapter_version": (str, type(None)),
    "workflow_status": (str, type(None)),
    "overall_status": (str, type(None)),
    "review_item_count": (int,),
    "warning_count": (int,),
    "unresolved_count": (int, type(None)),
    "latency_ms": (int, float),
}
_RUN_ALLOWED = set(_COMMON_SCALAR_FIELDS) | {"summary"}
_APPROVAL_ALLOWED = _RUN_ALLOWED | {"decision_counts", "reviewer_filled", "human_override"}

_SUMMARY_KEYS = ("total", "met", "unmet", "insufficient")
_DECISION_KEYS = ("confirm", "reject", "mark_insufficient")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workflow_trace_id(workflow_id: str | None) -> str:
    """workflow 与 trace 的稳定关联：由 workflow_id 不可逆派生，无需映射表。"""
    if not workflow_id:
        return uuid.uuid4().hex[:24]
    digest = hashlib.sha256(f"qualification-workflow:{workflow_id}".encode("utf-8")).hexdigest()
    return digest[:24]


def _project_ref(project_id: str | None) -> str | None:
    """project_id 的不可逆摘要：trace 文件中不保存原始项目 ID。"""
    if not project_id:
        return None
    return hashlib.sha256(f"qualification-project:{project_id}".encode("utf-8")).hexdigest()[:16]


def _new_event_id() -> str:
    return uuid.uuid4().hex


def _sanitize_summary(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key in _SUMMARY_KEYS:
        v = value.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out[key] = int(v)
    return out


def _sanitize_decision_counts(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key in _DECISION_KEYS:
        v = value.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out[key] = int(v)
    return out


def _desensitize(event: Any) -> dict:
    """严格白名单清洗：只保留文档约定字段，未知字段/嵌套全部删除。"""
    if not isinstance(event, dict):
        return {}
    event_type = event.get("event_type")
    if event_type == "run":
        allowed = _RUN_ALLOWED
    elif event_type == "approval":
        allowed = _APPROVAL_ALLOWED
    else:
        return {}  # 未知事件类型：整个丢弃
    clean: dict[str, Any] = {}
    for key in allowed:
        if key not in event:
            continue
        value = event[key]
        if key == "summary":
            clean[key] = _sanitize_summary(value)
        elif key == "decision_counts":
            clean[key] = _sanitize_decision_counts(value)
        elif key in ("reviewer_filled", "human_override"):
            if isinstance(value, bool):
                clean[key] = value
        else:
            allowed_types = _COMMON_SCALAR_FIELDS[key]
            if isinstance(value, allowed_types):
                clean[key] = value
    return clean


class TraceStore:
    """append-only JSONL 存储。所有写操作失败都返回 False，不抛异常。"""

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            settings = get_settings()
            base_dir = (
                Path(settings.qualification_trace_dir)
                if settings.qualification_trace_dir
                else (Path(settings.projects_root) / "_qualification_flywheel")
            )
        self.base_dir = Path(base_dir)
        self.file_path = self.base_dir / "flywheel.jsonl"

    def append(self, event: dict) -> bool:
        try:
            clean = _desensitize(event)
            if not clean:
                return False  # 清洗后为空（未知事件/字段）按失败处理，不落盘
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(clean, ensure_ascii=False) + "\n")
            return True
        except Exception:
            return False

    def read_events(
        self,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[dict]:
        """读取事件：跳过损坏行，返回白名单清洗后的事件。"""
        events: list[dict] = []
        if not self.file_path.exists():
            return events
        try:
            with open(self.file_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue  # 损坏行跳过
                    if isinstance(event, dict):
                        clean = _desensitize(event)
                        if clean:
                            events.append(clean)
        except Exception:
            return []
        if newest_first:
            events.reverse()
        if limit is not None and limit > 0:
            events = events[:limit]
        return events

    def reset(self) -> None:
        try:
            if self.file_path.exists():
                self.file_path.unlink()
        except Exception:
            pass


_active_store: TraceStore | None = None


def get_trace_store() -> TraceStore:
    global _active_store
    if _active_store is None:
        _active_store = TraceStore()
    return _active_store


def set_trace_store(store: TraceStore | None) -> None:
    """测试注入：替换 / 清空全局 TraceStore。"""
    global _active_store
    _active_store = store


def _base_event(
    event_type: str,
    *,
    trace_id: str,
    project_ref: str | None,
    entrypoint: str,
    workflow_status: str | None,
    overall_status: str | None,
    summary: dict,
    review_item_count: int,
    warning_count: int,
    unresolved_count: int | None,
    latency_ms: float,
) -> dict:
    return {
        "event_id": _new_event_id(),
        "event_type": event_type,
        "occurred_at": _now_iso(),
        "trace_id": trace_id,
        "project_ref": project_ref,
        "entrypoint": entrypoint,
        "matcher_version": MATCHER_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "workflow_status": workflow_status,
        "overall_status": overall_status,
        "summary": summary,
        "review_item_count": review_item_count,
        "warning_count": warning_count,
        "unresolved_count": unresolved_count,
        "latency_ms": round(latency_ms, 2),
    }


def record_run_trace(
    *,
    entrypoint: str,
    project_id: str | None,
    workflow_id: str | None,
    workflow_status: str | None,
    report: Any,
    review_items: list[Any],
    warnings: list[str],
    unresolved_count: int | None,
    latency_ms: float,
) -> tuple[str, list[str]]:
    """记录一次 run trace。返回 (trace_id, 降级 warnings)。

    workflow_status 由调用方传入 workflow 实际状态（含 force_review 强制 waiting_human 场景），
    /match 等无 workflow 的入口传 None。事件中不保存原始 project_id / workflow_id。
    """
    trace_id = _workflow_trace_id(workflow_id)
    event = _base_event(
        "run",
        trace_id=trace_id,
        project_ref=_project_ref(project_id),
        entrypoint=entrypoint,
        workflow_status=workflow_status,
        overall_status=getattr(report, "overall_status", None),
        summary=summary_of(report),
        review_item_count=len(review_items),
        warning_count=len(warnings),
        unresolved_count=unresolved_count,
        latency_ms=round(latency_ms, 2),
    )
    ok = get_trace_store().append(event)
    return trace_id, ([TRACE_WRITE_FAILED_WARNING] if not ok else [])


def record_approval_trace(
    *,
    workflow_id: str,
    project_id: str | None,
    workflow: Any,
    new_decisions: list[dict],
    latency_ms: float,
) -> tuple[str, list[str]]:
    """记录一次人工审批事件（新决策批次）。返回 (trace_id, 降级 warnings)。"""
    trace_id = _workflow_trace_id(workflow_id)
    orig_status = {item.requirement_id: item.status for item in workflow.review_items}
    decision_counts: dict[str, int] = {"confirm": 0, "reject": 0, "mark_insufficient": 0}
    reviewer_filled = False
    human_override = False
    for d in new_decisions:
        decision = d.get("decision", "")
        if decision in decision_counts:
            decision_counts[decision] += 1
        if str(d.get("reviewer", "")).strip():
            reviewer_filled = True
        original = orig_status.get(d.get("requirement_id"))
        if decision == "reject" and original != "unmet":
            human_override = True
        elif decision == "mark_insufficient" and original != "insufficient":
            human_override = True
    event = _base_event(
        "approval",
        trace_id=trace_id,
        project_ref=_project_ref(project_id),
        entrypoint=getattr(workflow, "entrypoint", None) or "manual",
        workflow_status=getattr(workflow, "status", None),
        overall_status=getattr(workflow.report, "overall_status", None),
        summary=summary_of(workflow.report),
        review_item_count=len(workflow.review_items),
        warning_count=len(workflow.warnings),
        unresolved_count=None,
        latency_ms=round(latency_ms, 2),
    )
    event["decision_counts"] = decision_counts
    event["reviewer_filled"] = reviewer_filled
    event["human_override"] = human_override
    ok = get_trace_store().append(event)
    return trace_id, ([TRACE_WRITE_FAILED_WARNING] if not ok else [])


def summary_of(report: Any) -> dict:
    s = getattr(report, "summary", None)
    if s is None:
        return {"total": 0, "met": 0, "unmet": 0, "insufficient": 0}
    return {
        "total": int(getattr(s, "total", 0)),
        "met": int(getattr(s, "met", 0)),
        "unmet": int(getattr(s, "unmet", 0)),
        "insufficient": int(getattr(s, "insufficient", 0)),
    }


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def compute_metrics(events: list[dict]) -> dict:
    """从事件列表计算飞轮指标（无数据时全部安全返回 0）。"""
    runs = [e for e in events if e.get("event_type") == "run"]
    approvals = [e for e in events if e.get("event_type") == "approval"]
    run_count = len(runs)
    workflow_runs = [r for r in runs if r.get("workflow_status") in ("completed", "waiting_human")]
    auto_runs = [r for r in workflow_runs if r.get("workflow_status") == "completed"]
    human_runs = [r for r in workflow_runs if r.get("workflow_status") == "waiting_human"]
    insufficient_runs = [r for r in runs if r.get("overall_status") == "insufficient"]
    override_approvals = [a for a in approvals if a.get("human_override")]

    entrypoint_counts: dict[str, int] = {"manual": 0, "from_analysis": 0, "from_project": 0}
    for r in runs:
        ep = r.get("entrypoint")
        if ep in entrypoint_counts:
            entrypoint_counts[ep] += 1
        else:
            entrypoint_counts[ep] = entrypoint_counts.get(ep, 0) + 1

    status_counts: dict[str, int] = {"met": 0, "unmet": 0, "insufficient": 0}
    for r in runs:
        st = r.get("overall_status")
        if st in status_counts:
            status_counts[st] += 1
        else:
            status_counts[st] = status_counts.get(st, 0) + 1

    return {
        "run_count": run_count,
        "approval_run_count": len({a.get("trace_id") for a in approvals if a.get("trace_id")}),
        "auto_complete_rate": _rate(len(auto_runs), len(workflow_runs)),
        "human_intervention_rate": _rate(len(human_runs), len(workflow_runs)),
        "insufficient_rate": _rate(len(insufficient_runs), run_count),
        "human_override_rate": _rate(len(override_approvals), len(approvals)),
        "entrypoint_counts": entrypoint_counts,
        "status_counts": status_counts,
    }


__all__ = [
    "MATCHER_VERSION",
    "ADAPTER_VERSION",
    "TRACE_WRITE_FAILED_WARNING",
    "TraceStore",
    "get_trace_store",
    "set_trace_store",
    "record_run_trace",
    "record_approval_trace",
    "compute_metrics",
    "_desensitize",
]
