from __future__ import annotations

import logging

from core.llm_gateway.gateway import LLMGateway
from core.settings import get_settings

logger = logging.getLogger(__name__)

_gateway: LLMGateway | None = None


def get_llm_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        settings = get_settings()
        fallbacks = [m.strip() for m in settings.llm_fallback_modes.split(",") if m.strip()]
        if fallbacks:
            # P8-1（G-0）：配置了降级模型时提示需确保可达——降级目标不可达时
            # 主模型失败会叠加 404 二次失败并拖累熔断计数（phase8 排查结论）。
            logger.warning(
                "[LLM] 已配置降级模型 %s：请确保其端点可达，否则主模型失败后将叠加降级二次失败", fallbacks
            )
        _gateway = LLMGateway(
            {
                "providers": [
                    {
                        "api_key": settings.llm_api_key,
                        "api_base": settings.llm_api_base,
                    }
                ],
                "default_model": settings.llm_default_model,
                "fallback_models": fallbacks,
                "max_retries": settings.llm_max_retries,
                "max_concurrency": settings.llm_max_concurrency,  # BUG-6: 全局并发闸门
                # P8-4 补接线（2026-09-02）：此前 llm_timeout 从未传入网关，客户端一直
                # 用网关默认 60s——settings 改 180s 实际不生效（死配置）。
                "timeout": settings.llm_timeout,
                "enable_thinking": settings.llm_enable_thinking,  # qwen3.7 默认开思考，显式关闭提速
            }
        )
    return _gateway


def reset_llm_gateway():
    global _gateway
    _gateway = None
