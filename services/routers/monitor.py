"""监控 API：Tracing 指标、最近 span、状态。"""

from __future__ import annotations

from fastapi import APIRouter

from core.settings import get_settings
from core.tracing import get_tracer

router = APIRouter(prefix="/monitor", tags=["监控"])


@router.get("/metrics")
async def metrics(window_minutes: int = 60, kind: str | None = None):
    return get_tracer().metrics(window_minutes=window_minutes, kind=kind)


@router.get("/spans")
async def spans(limit: int = 100, kind: str | None = None):
    return {"spans": get_tracer().recent_spans(limit=limit, kind=kind)}


@router.get("/status")
async def monitor_status():
    s = get_settings()
    tracer = get_tracer()
    return {
        "enabled": s.trace_enabled,
        "trace_dir": s.trace_dir,
        "otlp_endpoint": bool(s.otlp_endpoint),
        "memory_spans": len(tracer.recent_spans(limit=100000)),
        "persisted": (s.trace_dir and (__import__("pathlib").Path(s.trace_dir) / "traces.jsonl").exists()),
    }
