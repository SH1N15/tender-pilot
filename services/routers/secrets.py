"""平台密钥管理路由（roadmap P1-前置）。

- 白名单内字段允许通过设置页写入 Windows 凭据管理器（keyring），保存后立即生效；
- 所有响应只返回掩码（`mask_secret`，只露末 4 位），绝不返回/记录明文；
- 写入/删除仅限系统管理员（复用 `services.middleware.rbac_middleware.get_current_user`）。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.secret_resolver import (
    delete_keyring_secret,
    mask_secret,
    resolve_secret,
    write_keyring_secret,
)
from services.middleware.rbac_middleware import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# 允许通过 API 写入的密钥白名单：环境变量名 → secret_resolver 字段名
ALLOWED_SECRET_FIELDS: dict[str, str] = {
    "BMP_EMBEDDING_API_KEY": "embedding_api_key",
    "BMP_LLM_API_KEY": "llm_api_key",
    "BMP_OCR_API_KEY": "ocr_api_key",
    "BMP_IMAGE_API_KEY": "image_api_key",
}


def _scrub(message: str, *secrets: str | None) -> str:
    """从异常/错误消息中擦除任何明文密钥片段，防止泄漏到响应或日志。"""
    cleaned = message
    for s in secrets:
        if s and len(s) >= 4:
            cleaned = cleaned.replace(s, "****")
    return cleaned


async def require_admin(user=Depends(get_current_user)):
    """仅系统管理员可写密钥（与 RBAC 中间件判定语义一致：user.role == "admin"）。"""
    if getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="权限不足: 仅系统管理员可管理密钥")
    return user


@router.get("/status")
async def secrets_status():
    """白名单内各密钥的配置状态（只含来源与掩码，不含明文）。"""
    items = {}
    for env_name, field_name in ALLOWED_SECRET_FIELDS.items():
        try:
            value, source = resolve_secret(field_name)
        except Exception:  # noqa: BLE001
            value, source = None, "missing"
        configured = bool(value)
        items[env_name] = {
            "configured": configured,
            "source": source if configured else "missing",
            "masked": mask_secret(value) if configured else None,
        }
    return {"secrets": items}


class SecretWriteBody(BaseModel):
    value: str = ""


@router.put("/{field}")
async def put_secret(field: str, body: SecretWriteBody, _user=Depends(require_admin)):
    """写入/清除白名单内的密钥；value 为空串表示清除（回退到 .env / 环境变量语义）。

    明文 value 只进 keyring，不写日志、不进异常消息、不进响应。
    """
    field_name = ALLOWED_SECRET_FIELDS.get(field)
    if field_name is None:
        raise HTTPException(status_code=404, detail="不支持的密钥字段")

    value = body.value or ""
    if not value.strip():
        ok = delete_keyring_secret(field)
        if not ok:
            # 无条目或 keyring 不可用都视为已回退到 .env/环境变量语义
            logger.info("清除 keyring 条目 %s（无条目或不可用，视为成功）", field)
        return {"success": True, "masked": None, "cleared": True}

    ok = write_keyring_secret(field, value)
    if not ok:
        raise HTTPException(status_code=500, detail="写入凭据管理器失败（keyring 不可用）")
    logger.info("密钥 %s 已更新（掩码 %s）", field, mask_secret(value))
    return {"success": True, "masked": mask_secret(value), "cleared": False}


class EmbeddingConfigBody(BaseModel):
    model: str | None = None
    api_base: str | None = None


@router.get("/embedding/config")
async def get_embedding_config():
    """Embedding 非密配置（模型名 / API Base）；key 只回掩码。"""
    from core.settings import get_settings

    try:
        api_key, key_source = resolve_secret("embedding_api_key")
    except Exception:  # noqa: BLE001
        api_key, key_source = None, "missing"
    if not api_key:
        settings = get_settings()
        api_key = settings.embedding_api_key or None
        key_source = "env" if api_key else "missing"

    settings = get_settings()
    env = services_env_read()
    return {
        "model": settings.embedding_model,
        "api_base": settings.embedding_api_base,
        "api_key_masked": mask_secret(api_key) if api_key else None,
        "api_key_set": bool(api_key),
        "api_key_source": key_source,
        "source": "env" if ("BMP_EMBEDDING_MODEL" in env or "BMP_EMBEDDING_API_BASE" in env) else "default",
    }


def services_env_read() -> dict[str, str]:
    import services.env_store

    return services.env_store.read_env()


@router.put("/embedding/config")
async def put_embedding_config(body: EmbeddingConfigBody, _user=Depends(require_admin)):
    """写入 Embedding 模型名 / API Base（非密配置，写入 .env），仅管理员。"""
    import services.env_store
    from core.settings import reload_settings

    updates: dict[str, str] = {}
    if body.model is not None and body.model.strip():
        updates["BMP_EMBEDDING_MODEL"] = body.model.strip()
    if body.api_base is not None and body.api_base.strip():
        updates["BMP_EMBEDDING_API_BASE"] = body.api_base.strip()
    if not updates:
        return {"success": False, "error": "未提供任何可更新字段"}

    try:
        services.env_store.write_env_atomic(updates)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"写入 .env 失败: {e}") from e

    reload_settings()

    from core.settings import get_settings

    settings = get_settings()
    return {
        "success": True,
        "model": updates.get("BMP_EMBEDDING_MODEL") or settings.embedding_model,
        "api_base": updates.get("BMP_EMBEDDING_API_BASE") or settings.embedding_api_base,
        "needs_restart": False,
    }


@router.post("/embedding/test")
async def test_embedding(body: EmbeddingConfigBody | None = None):
    """用当前解析到的 Embedding Key 发一次最小真实请求，验证连通性。

    body 可选传 model / api_base（表单当前值），便于保存前先测试。
    """
    from core.settings import get_settings

    try:
        api_key, _source = resolve_secret("embedding_api_key")
    except Exception:  # noqa: BLE001
        api_key, _source = None, "missing"
    if not api_key:
        raise HTTPException(status_code=400, detail="Embedding API Key 未配置，请先在设置页填写并保存")

    settings = get_settings()
    model_override = body.model.strip() if body and body.model else ""
    base_override = body.api_base.strip() if body and body.api_base else ""
    model = model_override or settings.embedding_model
    api_base = base_override or settings.embedding_api_base

    from core.cost_guard import CircuitOpenError, QuotaExceededError, get_cost_guard

    guard = get_cost_guard()
    try:
        await guard.precheck("llm")  # embedding 属外部 LLM 家族调用，走 LLM 配额/熔断
    except (CircuitOpenError, QuotaExceededError) as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

    started = time.perf_counter()
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        response = await client.embeddings.create(model=model, input=["连通性测试"])
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not response.data:
            raise ValueError("响应中无 embedding 数据")
        await guard.record_result("llm", ok=True)
        return {"ok": True, "latency_ms": latency_ms, "model": model}
    except Exception as e:  # noqa: BLE001
        await guard.record_result("llm", ok=False)
        detail = _scrub(str(e), api_key)
        raise HTTPException(status_code=502, detail=f"Embedding 连接失败: {detail}") from e


# ── P-C: Reranker 配置（走 env_store 读写与 .dev/env_writes.log 审计）────────


def _reranker_settings_snapshot() -> dict:
    from core.settings import get_settings

    settings = get_settings()
    return {
        "enabled": settings.reranker_enabled,
        "model": settings.reranker_model,
        "api_base": settings.reranker_api_base,
    }


class RerankerConfigBody(BaseModel):
    enabled: bool | None = None
    model: str | None = None
    api_base: str | None = None


class ImageConfigBody(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    api_base: str | None = None
    model: str | None = None


def _image_settings_snapshot() -> dict:
    from core.settings import get_settings

    settings = get_settings()
    try:
        image_key, key_source = resolve_secret("image_api_key")
    except Exception:  # noqa: BLE001
        image_key, key_source = None, "missing"
    if not image_key:
        image_key = settings.image_api_key or None
        key_source = "env" if image_key else "missing"
    return {
        "enabled": bool(settings.illustration_enabled),
        "provider": settings.image_provider,
        "api_base": settings.image_base_url,
        "model": settings.image_model,
        "api_key_masked": mask_secret(image_key) if image_key else None,
        "api_key_set": bool(image_key),
        "api_key_source": key_source,
    }


@router.get("/image/config")
async def get_image_config():
    """AI 配图配置：非密字段可见，Key 只返回掩码。"""
    return _image_settings_snapshot()


@router.put("/image/config")
async def put_image_config(body: ImageConfigBody, _user=Depends(require_admin)):
    """保存 AI 配图非密配置；Key 通过 /secrets/BMP_IMAGE_API_KEY 写入 keyring。"""
    import services.env_store
    from core.settings import reload_settings

    updates: dict[str, str] = {}
    if body.enabled is not None:
        updates["BMP_ILLUSTRATION_ENABLED"] = "true" if body.enabled else "false"
    if body.provider is not None and body.provider.strip():
        updates["BMP_IMAGE_PROVIDER"] = body.provider.strip()
    if body.api_base is not None:
        updates["BMP_IMAGE_BASE_URL"] = body.api_base.strip()
    if body.model is not None:
        updates["BMP_IMAGE_MODEL"] = body.model.strip()
    if updates:
        try:
            services.env_store.write_env_atomic(updates)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"写入图片配置失败: {e}") from e
        reload_settings()
    return {"success": True, **_image_settings_snapshot()}


@router.get("/reranker/config")
async def get_reranker_config():
    """Reranker 非密配置；key 只回掩码与来源。"""
    try:
        api_key, key_source = resolve_secret("reranker_api_key")
    except Exception:  # noqa: BLE001
        api_key, key_source = None, "missing"
    env = services_env_read()
    return {
        **_reranker_settings_snapshot(),
        "api_key_masked": mask_secret(api_key) if api_key else None,
        "api_key_set": bool(api_key),
        "api_key_source": key_source,
        "source": "env" if any(k in env for k in ("BMP_RERANKER_MODEL", "BMP_RERANKER_API_BASE",
            "BMP_RERANKER_ENABLED")) else "default",
    }


@router.put("/reranker/config")
async def put_reranker_config(body: RerankerConfigBody, _user=Depends(require_admin)):
    """写入 Reranker enabled/model/api_base（.env 原子写，经审计）。"""
    import services.env_store
    from core.settings import reload_settings

    updates: dict[str, str] = {}
    if body.enabled is not None:
        updates["BMP_RERANKER_ENABLED"] = "true" if body.enabled else "false"
    if body.model is not None and body.model.strip():
        updates["BMP_RERANKER_MODEL"] = body.model.strip()
    if body.api_base is not None and body.api_base.strip():
        updates["BMP_RERANKER_API_BASE"] = body.api_base.strip()
    if not updates:
        return {"success": False, "error": "未提供任何可更新字段"}
    try:
        services.env_store.write_env_atomic(updates)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"写入 .env 失败: {e}") from e
    reload_settings()
    return {"success": True, **_reranker_settings_snapshot()}


class RerankerKeyBody(BaseModel):
    value: str = ""


@router.put("/reranker/key")
async def put_reranker_key(body: RerankerKeyBody, _user=Depends(require_admin)):
    """写入/清除 Reranker Key（明文只进 .env 写入路径，经 env_store 审计；响应只回掩码）。"""
    import services.env_store
    from core.settings import reload_settings

    value = body.value or ""
    if not value.strip():
        services.env_store.write_env_atomic(deletes=["BMP_RERANKER_API_KEY"])
        reload_settings()
        return {"success": True, "masked": None, "cleared": True}
    services.env_store.write_env_atomic({"BMP_RERANKER_API_KEY": value.strip()})
    reload_settings()
    logger.info("Reranker Key 已更新（掩码 %s）", mask_secret(value))
    return {"success": True, "masked": mask_secret(value), "cleared": False}


@router.post("/reranker/test")
async def test_reranker(body: RerankerConfigBody | None = None):
    """用当前 Reranker 配置发一次最小真实 /rerank 请求，验证连通性。"""
    try:
        api_key, _source = resolve_secret("reranker_api_key")
    except Exception:  # noqa: BLE001
        api_key, _src = None, "missing"
    if not api_key:
        raise HTTPException(status_code=400, detail="Reranker API Key 未配置，请先在设置页填写并保存")

    settings = _reranker_settings_snapshot()
    model = (body.model.strip() if body and body.model else "") or settings["model"]
    api_base = (body.api_base.strip() if body and body.api_base else "") or settings["api_base"]

    started = time.perf_counter()
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{api_base.rstrip('/')}/rerank",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "query": "连通性测试",
                    "documents": ["BidMaster 智能招投标平台检索重排连通性测试文本。"],
                    "top_n": 1,
                    "return_documents": False,
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                raise ValueError("rerank 响应中无 results")
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": True, "latency_ms": latency_ms, "model": model}
    except Exception as e:  # noqa: BLE001
        detail = _scrub(str(e), api_key)
        raise HTTPException(status_code=502, detail=f"Reranker 连接失败: {detail}") from e
