"""轻量 Tracing / 监控（vNext）。

设计：
- 进程内 Span 记录（内存环形缓冲）+ JSONL 持久化（原子追加），不依赖 Prometheus/Grafana；
- 可选外部 OTLP 导出（OpenTelemetry 官方 SDK），未配置 endpoint 时零外部依赖；
- 隐私边界：Span 属性只允许白名单键；任何键名含 key/token/secret/password/auth 的一律剔除，
  绝不记录 API Key、完整投标正文或企业敏感材料；
- kind 取值：http / agent / llm / tool / skill / ocr / a2a / agui / workflow。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_span_stack: contextvars.ContextVar[list] = contextvars.ContextVar("bidmaster_span_stack", default=[])

# 隐私：允许写入的属性键白名单（小写匹配）
_ALLOWED_ATTR_KEYS = {
    "span.kind",
    "http.method",
    "http.path",
    "http.status_code",
    "agent.name",
    "agent.task_len",
    "pipeline",
    "step",
    "llm.model",
    "llm.provider",
    "llm.prompt_tokens",
    "llm.completion_tokens",
    "llm.total_tokens",
    "tool.name",
    "tool.args_len",
    "tool.result_len",
    "skill.name",
    "skill.success",
    "ocr.mode",
    "ocr.task_id",
    "ocr.pages",
    "ocr.duration_ms",
    "ocr.error_class",
    "a2a.method",
    "a2a.task_state",
    "a2a.protocol_version",
    "agui.event",
    "agui.run_id",
    "agui.thread_id",
    "project.id",
    "project.ref",
    "workflow.id",
    "trace.id",
    "run.id",
    "error.type",
    "error.message_len",
    "error.message",
    "target",
    "db.ready",
    "db.name",
    "mcp.tool",
    "mcp.transport",
}
_FORBIDDEN_SUBSTR = ("key", "token", "secret", "password", "auth", "credential")


@dataclass
class Span:
    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    kind: str
    start_ns: int = field(default_factory=time.perf_counter_ns)
    end_ns: int | None = None
    status: str = "ok"  # ok | error
    error_type: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, int] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        end = self.end_ns or time.perf_counter_ns()
        return round((end - self.start_ns) / 1_000_000, 3)


def _sanitize_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        low = key.lower()
        if any(s in low for s in _FORBIDDEN_SUBSTR):
            continue
        if low not in _ALLOWED_ATTR_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            # P8-4（G-0）：字符串属性统一截断 300 字符（error.message 等上游错误
            # 文本可能很长）；白名单本身不含任何 key/token 类键，无泄露面。
            if isinstance(value, str) and len(value) > 300:
                value = value[:300]
            out[key] = value
        else:
            out[key] = str(value)[:200]
    return out


class Tracer:
    """轻量 Tracer：内存环形缓冲 + JSONL 原子追加 + 可选 OTLP 导出。"""

    _instance: "Tracer | None" = None

    def __init__(self, max_memory_spans: int = 5000):
        self._spans: list[Span] = []
        self._max = max_memory_spans
        self._jsonl_path: Path | None = None
        self._otlp = None

    @classmethod
    def instance(cls) -> "Tracer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def configure(self, trace_dir: str | None = None, otlp_endpoint: str | None = None) -> None:
        if trace_dir:
            self._jsonl_path = Path(trace_dir) / "traces.jsonl"
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        if otlp_endpoint:
            try:
                from core.tracing.otel import OTLPExporter

                self._otlp = OTLPExporter(otlp_endpoint)
            except Exception as e:  # noqa: BLE001
                logger.warning("OTLP 导出器初始化失败，降级为内存/JSONL: %s", e)
                self._otlp = None

    def start_span(self, name: str, kind: str = "http", attributes: dict[str, Any] | None = None) -> Span:
        stack = _span_stack.get()
        parent = stack[-1] if stack else None
        span = Span(
            span_id=uuid.uuid4().hex[:16],
            trace_id=parent.trace_id if parent else uuid.uuid4().hex[:16],
            parent_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
            attributes=_sanitize_attributes(attributes or {}),
        )
        stack.append(span)
        _span_stack.set(stack)
        return span

    def end_span(
        self,
        span: Span,
        status: str = "ok",
        error_type: str | None = None,
        attributes: dict[str, Any] | None = None,
        token_usage: dict[str, int] | None = None,
    ) -> None:
        span.end_ns = time.perf_counter_ns()
        span.status = status
        span.error_type = error_type
        span.attributes.update(_sanitize_attributes(attributes or {}))
        if token_usage:
            span.token_usage = {
                k: int(v) for k, v in token_usage.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        self._record(span)
        stack = _span_stack.get()
        if stack and stack[-1].span_id == span.span_id:
            stack.pop()
            _span_stack.set(stack)
        else:
            _span_stack.set([])

    def span(self, name: str, kind: str = "http", attributes: dict[str, Any] | None = None) -> "_SpanContext":
        return _SpanContext(self, name, kind, attributes)

    def _record(self, span: Span) -> None:
        self._spans.append(span)
        if len(self._spans) > self._max:
            self._spans = self._spans[-self._max :]
        self._persist(span)

    def _persist(self, span: Span) -> None:
        if self._jsonl_path is None:
            return
        try:
            payload = {
                "span_id": span.span_id,
                "trace_id": span.trace_id,
                "parent_id": span.parent_id,
                "name": span.name,
                "kind": span.kind,
                "start": round(span.start_ns / 1_000_000, 3),
                "duration_ms": span.duration_ms,
                "status": span.status,
                "error_type": span.error_type,
                "attributes": _sanitize_attributes(span.attributes),
                "token_usage": span.token_usage,
            }
            line = json.dumps(payload, ensure_ascii=False)
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:  # noqa: BLE001
            logger.debug("Trace 持久化失败（忽略）: %s", e)

    def flush_otlp(self) -> None:
        if self._otlp is not None:
            self._otlp.export_batch(self._spans[-200:])

    def recent_spans(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        spans = self._spans
        if kind:
            spans = [s for s in spans if s.kind == kind]
        return [self._to_dict(s) for s in spans[-limit:]]

    def _to_dict(self, span: Span) -> dict[str, Any]:
        return {
            "span_id": span.span_id,
            "trace_id": span.trace_id,
            "parent_id": span.parent_id,
            "name": span.name,
            "kind": span.kind,
            "start_ms": round(span.start_ns / 1_000_000, 3),
            "duration_ms": span.duration_ms,
            "status": span.status,
            "error_type": span.error_type,
            "attributes": dict(span.attributes),
            "token_usage": dict(span.token_usage),
        }

    def metrics(self, window_minutes: int = 60, kind: str | None = None) -> dict[str, Any]:
        """成功率、P50/P95 延迟、Token、错误分类。"""
        now = time.perf_counter_ns()
        cutoff = now - window_minutes * 60 * 1_000_000_000
        spans = [s for s in self._spans if s.start_ns >= cutoff]
        if kind:
            spans = [s for s in spans if s.kind == kind]

        by_kind: dict[str, dict[str, Any]] = {}
        for s in spans:
            bucket = by_kind.setdefault(
                s.kind,
                {
                    "count": 0,
                    "ok": 0,
                    "error": 0,
                    "durations": [],
                    "errors": {},
                    "tokens": 0,
                },
            )
            bucket["count"] += 1
            if s.status == "ok":
                bucket["ok"] += 1
            else:
                bucket["error"] += 1
                err = s.error_type or "unknown"
                bucket["errors"][err] = bucket["errors"].get(err, 0) + 1
            bucket["durations"].append(s.duration_ms)
            bucket["tokens"] += s.token_usage.get("total_tokens", 0)

        def _summarize(bucket: dict[str, Any]) -> dict[str, Any]:
            durations = sorted(bucket["durations"])
            n = len(durations)
            p50 = durations[n // 2] if n else 0.0
            p95 = durations[int(n * 0.95)] if n else 0.0
            return {
                "count": bucket["count"],
                "ok": bucket["ok"],
                "error": bucket["error"],
                "success_rate": round(bucket["ok"] / bucket["count"], 4) if bucket["count"] else 1.0,
                "p50_ms": round(p50, 3),
                "p95_ms": round(p95, 3),
                "errors": bucket["errors"],
                "total_tokens": bucket["tokens"],
            }

        return {
            "window_minutes": window_minutes,
            "overall": _summarize(
                {
                    "count": len(spans),
                    "ok": sum(1 for s in spans if s.status == "ok"),
                    "error": sum(1 for s in spans if s.status != "ok"),
                    "durations": [s.duration_ms for s in spans],
                    "errors": {},
                    "tokens": sum(s.token_usage.get("total_tokens", 0) for s in spans),
                }
            ),
            "by_kind": {k: _summarize(v) for k, v in by_kind.items()},
        }


class _SpanContext:
    def __init__(self, tracer: Tracer, name: str, kind: str, attributes: dict[str, Any] | None):
        self._tracer = tracer
        self._name = name
        self._kind = kind
        self._attributes = attributes or {}
        self._span: Span | None = None

    async def __aenter__(self) -> Span:
        self._span = self._tracer.start_span(self._name, self._kind, self._attributes)
        return self._span

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._span is not None:
            self._tracer.end_span(
                self._span,
                status="error" if exc_type is not None else "ok",
                error_type=exc.__class__.__name__ if exc else None,
            )


def get_tracer() -> Tracer:
    return Tracer.instance()
