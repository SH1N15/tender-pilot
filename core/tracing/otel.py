"""可选 OTLP 导出：把本地 Span 桥接到 OpenTelemetry 官方 SDK 并发送到外部 Collector。

未配置 BMP_OTLP_ENDPOINT 时不会实例化，不影响默认运行。
"""

from __future__ import annotations

import logging

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanKind, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanContext, TraceFlags
from opentelemetry.trace.status import Status, StatusCode

logger = logging.getLogger(__name__)

_KIND_MAP = {
    "http": SpanKind.SERVER,
    "agent": SpanKind.INTERNAL,
    "llm": SpanKind.CLIENT,
    "tool": SpanKind.CLIENT,
    "skill": SpanKind.INTERNAL,
    "ocr": SpanKind.CLIENT,
    "a2a": SpanKind.SERVER,
    "agui": SpanKind.SERVER,
    "workflow": SpanKind.INTERNAL,
}


class OTLPExporter:
    def __init__(self, endpoint: str):
        self._provider = TracerProvider(resource=Resource.create({"service.name": "bidmaster-pro"}))
        self._exporter = OTLPSpanExporter(endpoint=endpoint)
        self._provider.add_span_processor(BatchSpanProcessor(self._exporter))
        self._tracer = self._provider.get_tracer("bidmaster-pro")
        logger.info("OTLP exporter 已配置: %s", endpoint)

    def export_batch(self, spans: list) -> None:
        for span in spans:
            try:
                self._export_one(span)
            except Exception as e:  # noqa: BLE001
                logger.debug("OTLP 导出单条失败: %s", e)

    def _export_one(self, span) -> None:
        ctx = SpanContext(
            trace_id=int(span.trace_id[:16], 16),
            span_id=int(span.span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        with self._tracer.start_as_current_span(
            span.name,
            context=ctx,
            kind=_KIND_MAP.get(span.kind, SpanKind.INTERNAL),
            start_time=int(span.start_ns // 1000),
        ) as otel_span:
            otel_span.set_attribute("bidmaster.kind", span.kind)
            for key, value in span.attributes.items():
                otel_span.set_attribute(key, value)
            if span.status == "ok":
                otel_span.set_status(Status(StatusCode.OK))
            else:
                otel_span.set_status(Status(StatusCode.ERROR, span.error_type or "error"))
            otel_span.end(end_time=int((span.end_ns or span.start_ns) // 1000))
