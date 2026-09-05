"""FastAPI tracing middleware：为每个 /api 请求记录 http span。"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.tracing import get_tracer


class TracingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, exclude_prefixes: tuple[str, ...] = ("/api/health", "/api/monitor/spans")):
        super().__init__(app)
        self._exclude = exclude_prefixes

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        tracer = get_tracer()
        span = tracer.start_span(
            f"{request.method} {path}",
            "http",
            {"http.method": request.method, "http.path": path[:200]},
        )
        try:
            response: Response = await call_next(request)
            tracer.end_span(
                span,
                status="ok" if response.status_code < 500 else "error",
                error_type=None if response.status_code < 500 else f"http_{response.status_code}",
                attributes={"http.status_code": response.status_code},
            )
            return response
        except Exception as e:  # noqa: BLE001
            tracer.end_span(span, status="error", error_type=e.__class__.__name__)
            raise
