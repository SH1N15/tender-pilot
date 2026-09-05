"""外部调用成本守卫（roadmap P0-5）。

统一为 LLM / MinerU OCR / AI 配图提供：日配额（次数+token）+ 超时 + 熔断。
- 计数存储：Redis（BMP_REDIS_URL）优先；不可用回退进程内存并打 warning（只告警一次）；
- 熔断复用 core/agent_framework/circuit_breaker.py：连续 N 次失败（默认 5）打开，
  冷却期（默认 60s）后自动半开试探；
- 超限/熔断抛中文可读异常，HTTP 层转 429/503；拒绝时不产生任何真实外部请求。
"""

from __future__ import annotations

import logging
from datetime import date

from core.agent_framework.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

_KIND_LABELS = {"llm": "LLM", "ocr": "OCR", "image": "AI配图"}


class CostGuardError(Exception):
    """成本守卫异常基类；status_code 供 HTTP 层转换。"""

    status_code = 429


class QuotaExceededError(CostGuardError):
    """日配额超限 → HTTP 429。"""

    status_code = 429


class CircuitOpenError(CostGuardError):
    """熔断打开 → HTTP 503。"""

    status_code = 503


class CostGuard:
    def __init__(self, settings=None, redis_client=None):
        from core.settings import get_settings

        self._settings = settings or get_settings()
        self._redis = redis_client
        self._redis_checked = redis_client is not None
        self._redis_warned = False
        # 进程内存回退计数：{(kind, metric, day): value}
        self._memory_counters: dict[tuple[str, str, str], int] = {}
        # 熔断器复用现有实现（连续失败阈值 + 冷却后半开）
        self._breakers: dict[str, CircuitBreaker] = {}

    # ── 配置 ──────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "guard_enabled", True))

    def _max_calls(self, kind: str) -> int:
        if kind == "llm":
            return int(getattr(self._settings, "llm_daily_max_calls", 2000))
        if kind == "ocr":
            return int(getattr(self._settings, "ocr_daily_max_calls", 500))
        return int(getattr(self._settings, "image_daily_max_calls", 500))

    def _max_tokens(self, kind: str) -> int:
        if kind == "llm":
            return int(getattr(self._settings, "llm_daily_max_tokens", 2_000_000))
        return 0

    def _breaker(self, kind: str, key: str | None = None) -> CircuitBreaker:
        # P8-3（G-0）：LLM 熔断按模型/端点分键——此前全局单键，15 维批量任务中
        # 个别失败会拖垮整批（phase8：5 个失败即熔断，剩余维度全部秒失败）。
        breaker_key = f"{kind}:{key}" if key else kind
        if breaker_key not in self._breakers:
            threshold = int(getattr(self._settings, "guard_failure_threshold", 5))
            cooldown = float(getattr(self._settings, "guard_cooldown_seconds", 60.0))
            self._breakers[breaker_key] = CircuitBreaker(
                max_consecutive_failures=threshold, reset_timeout=cooldown
            )
        return self._breakers[breaker_key]

    # ── Redis / 内存计数 ─────────────────────────────────
    async def _get_redis(self):
        if not self._redis_checked:
            self._redis_checked = True
            try:
                import redis.asyncio as aioredis

                client = aioredis.from_url(self._settings.redis_url, socket_connect_timeout=2)
                await client.ping()
                self._redis = client
                logger.info("CostGuard 计数存储：Redis (%s)", self._settings.redis_url.split("@")[-1])
            except Exception as e:  # noqa: BLE001
                self._redis = None
                if not self._redis_warned:
                    self._redis_warned = True
                    logger.warning("CostGuard Redis 不可用，回退进程内存计数: %s", e)
        return self._redis

    @staticmethod
    def _day_key(kind: str, metric: str) -> str:
        return f"bmp:guard:{kind}:{metric}:{date.today().isoformat()}"

    async def _incr(self, kind: str, metric: str, amount: int = 1) -> int:
        """计数 +amount，返回累计值。Redis 不可用回退内存。"""
        redis = await self._get_redis()
        key = self._day_key(kind, metric)
        if redis is not None:
            try:
                value = await redis.incrby(key, amount)
                await redis.expire(key, 172800)  # 2 天，防堆积
                return int(value)
            except Exception as e:  # noqa: BLE001
                if not self._redis_warned:
                    self._redis_warned = True
                    logger.warning("CostGuard Redis 计数失败，回退进程内存: %s", e)
        mem_key = (kind, metric, date.today().isoformat())
        self._memory_counters[mem_key] = self._memory_counters.get(mem_key, 0) + amount
        return self._memory_counters[mem_key]

    async def _current(self, kind: str, metric: str) -> int:
        redis = await self._get_redis()
        key = self._day_key(kind, metric)
        if redis is not None:
            try:
                v = await redis.get(key)
                return int(v or 0)
            except Exception:  # noqa: BLE001
                pass
        return self._memory_counters.get((kind, metric, date.today().isoformat()), 0)

    # ── 对外 API ─────────────────────────────────────────
    async def precheck(self, kind: str, key: str | None = None) -> dict:
        """对外请求前必须调用。熔断打开 / 日配额超限则抛异常；否则占用 1 次调用额度。

        key：模型/端点分键（P8-3）。LLM 调用传模型名，A 模型熔断不拦截 B 模型。
        """
        label = _KIND_LABELS.get(kind, kind)
        if not self.enabled:
            return {"enabled": False}
        breaker = self._breaker(kind, key)
        if not breaker.can_execute(f"{kind}:{key}" if key else kind):
            raise CircuitOpenError(
                f"{label}服务（{key or '默认'}）连续失败已熔断，请稍后再试（冷却期 {breaker.reset_timeout:.0f} 秒）"
            )
        calls = await self._incr(kind, "calls")
        limit = self._max_calls(kind)
        if limit > 0 and calls > limit:
            raise QuotaExceededError(f"今日 {label} 调用次数已达上限（{limit} 次/天），请明日再试或联系管理员调整配额")
        return {"calls_today": calls}

    async def record_result(self, kind: str, ok: bool, tokens: int = 0, key: str | None = None) -> None:
        """记录外部调用结果；tokens 用于日 token 配额统计。key=模型/端点分键（P8-3）。"""
        breaker_key = f"{kind}:{key}" if key else kind
        breaker = self._breaker(kind, key)
        if ok:
            breaker.record_success(breaker_key)
        else:
            breaker.record_failure(breaker_key)
        if tokens > 0 and self.enabled:
            total = await self._incr(kind, "tokens", tokens)
            limit = self._max_tokens(kind)
            if limit > 0 and total > limit and breaker.can_execute(breaker_key):
                logger.warning("今日 %s token 用量已达上限 %s（当前 %s）", kind, limit, total)
                # 打到熔断，冷却后半开
                for _ in range(int(getattr(self._settings, "guard_failure_threshold", 5))):
                    breaker.record_failure(breaker_key)

    async def usage(self, kind: str) -> dict:
        """查询当日用量（诊断页用）。"""
        return {
            "calls_today": await self._current(kind, "calls"),
            "tokens_today": await self._current(kind, "tokens"),
            "max_calls": self._max_calls(kind),
            "max_tokens": self._max_tokens(kind),
        }

    def reset_memory(self) -> None:
        self._memory_counters.clear()
        self._breakers.clear()


_guard: CostGuard | None = None


def get_cost_guard() -> CostGuard:
    global _guard
    if _guard is None:
        _guard = CostGuard()
    return _guard


def reset_cost_guard() -> CostGuard:
    """重建守卫（写入配置后/测试用）。"""
    global _guard
    _guard = CostGuard()
    return _guard
