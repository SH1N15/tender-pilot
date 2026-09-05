"""图运行级成本记账（铁律2第四件套：tracing 成本记账）。

RunMetrics 由运行器每 run 创建，节点经闭包持有；
LLM 调用数由 LLM 计数代理（CountingLLM）统计，token 数从 core/tracing
的 llm span（gateway._record_usage）聚合。
"""

from __future__ import annotations

import time
from typing import Any


class RunMetrics:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.started_at = time.monotonic()
        self.nodes: dict[str, dict] = {}
        self._current: dict[str, float] = {}
        # P-D2：Evidence Grounding Gate 放行/拒绝统计（全 run 累计）
        self.grounding: dict[str, int] = {"total": 0, "passed": 0, "rejected": 0}

    def start_node(self, node: str) -> None:
        self._current[node] = time.monotonic()
        self.nodes.setdefault(node, {"llm_calls": 0, "tokens": 0, "duration_ms": 0.0})

    def end_node(self, node: str, started: float | None = None) -> None:
        start = started if started is not None else self._current.pop(node, None)
        if start is not None:
            self.nodes.setdefault(node, {"llm_calls": 0, "tokens": 0, "duration_ms": 0.0})
            self.nodes[node]["duration_ms"] += round((time.monotonic() - start) * 1000, 2)
        self._current.pop(node, None)

    def add_llm(self, node: str, calls: int = 1, tokens: int = 0) -> None:
        bucket = self.nodes.setdefault(node, {"llm_calls": 0, "tokens": 0, "duration_ms": 0.0})
        bucket["llm_calls"] += calls
        bucket["tokens"] += tokens

    def add_grounding(self, stats: dict) -> None:
        """累计 Gate 统计：{total, passed, rejected}（P-D2）。"""
        for key in self.grounding:
            self.grounding[key] += int(stats.get(key, 0) or 0)

    @property
    def total_llm_calls(self) -> int:
        return sum(b["llm_calls"] for b in self.nodes.values())

    @property
    def total_tokens(self) -> int:
        return sum(b["tokens"] for b in self.nodes.values())

    @property
    def total_duration_ms(self) -> float:
        return round((time.monotonic() - self.started_at) * 1000, 2)

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "nodes": {k: dict(v) for k, v in self.nodes.items()},
            "total_llm_calls": self.total_llm_calls,
            "total_tokens": self.total_tokens,
            "total_duration_ms": self.total_duration_ms,
            "grounding": dict(self.grounding),
        }


class CountingLLM:
    """LLM 网关透明计数代理：只计数并透传，不改变行为。"""

    def __init__(self, inner: Any, metrics: RunMetrics, default_node: str):
        self._inner = inner
        self._metrics = metrics
        self._node = default_node

    def for_node(self, node: str) -> "CountingLLM":
        return CountingLLM(self._inner, self._metrics, node)

    def _usage_snapshot(self) -> int:
        """记录调用前的 usage 事件数（优先网关累计列表，退化到 tracer llm span 数）。"""
        usage_list = getattr(self._inner, "_token_usage", None)
        if isinstance(usage_list, list):
            return len(usage_list)
        try:
            from core.tracing import get_tracer

            return len(get_tracer().recent_spans(limit=10000, kind="llm"))
        except Exception:  # noqa: BLE001
            return 0

    def _tokens_since(self, before: int) -> int:
        """聚合调用后新增 usage 事件的 total_tokens（并发下按新增条数近似归属）。"""
        usage_list = getattr(self._inner, "_token_usage", None)
        if isinstance(usage_list, list):
            new_entries = usage_list[before:]
        else:
            try:
                from core.tracing import get_tracer

                spans = get_tracer().recent_spans(limit=10000, kind="llm")
            except Exception:  # noqa: BLE001
                return 0
            new_entries = [s.get("token_usage") or {} for s in spans[before:]]
        return sum(int(e.get("total_tokens", 0) or 0) for e in new_entries if isinstance(e, dict))

    async def _counted(self, method: str, /, *args, **kwargs):
        node = self._node
        before = self._usage_snapshot()
        try:
            return await getattr(self._inner, method)(*args, **kwargs)
        finally:
            self._metrics.add_llm(node, 1, self._tokens_since(before))

    async def collect_json(self, messages: list[dict], **kwargs) -> dict:
        return await self._counted("collect_json", messages, **kwargs)

    async def chat(self, messages: list[dict], **kwargs) -> Any:
        return await self._counted("chat", messages, **kwargs)

    async def chat_with_tools(self, messages: list[dict], **kwargs) -> Any:
        return await self._counted("chat_with_tools", messages, **kwargs)

    def __getattr__(self, item):  # 其余属性透传
        return getattr(self._inner, item)
