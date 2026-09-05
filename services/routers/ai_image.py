from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.skill_engine.base import SkillContext
from services.generate.skills.ai_image_skill import AiImageSkill
from services.llm_factory import get_llm_gateway

router = APIRouter()

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


_BUILTIN_PROVIDERS = ("volcengine", "google", "fallback")


class ImageGenerateRequest(BaseModel):
    prompt: str
    provider: str = ""  # 空=取 settings 默认（BMP_IMAGE_PROVIDER）
    image_size: str = "landscape_16_9"
    api_key: str | None = None  # G-7 收尾：通用 key（自定义端点/内置均可，覆盖 settings）
    volcengine_api_key: str | None = None
    google_api_key: str | None = None


class ProviderConfig(BaseModel):
    volcengine_api_key: str | None = None
    google_api_key: str | None = None


@router.post("/generate")
async def generate_image(req: ImageGenerateRequest):
    skill = AiImageSkill()
    gateway = get_llm_gateway()

    from core.settings import get_settings

    settings = get_settings()
    from core.secret_resolver import resolve_secret

    resolved_image_key, _image_key_source = resolve_secret("image_api_key")
    effective_image_key = settings.image_api_key or resolved_image_key
    provider = req.provider or settings.image_provider or "fallback"
    parameters = {
        "prompt": req.prompt,
        "provider": provider,
        "image_size": req.image_size,
    }

    # G-7 收尾：key 解析顺序=请求体 api_key > 请求体供应商专用 key > env > settings（BMP_IMAGE_API_KEY）
    if req.api_key:
        parameters["api_key"] = req.api_key

    if req.volcengine_api_key:
        parameters["volcengine_api_key"] = req.volcengine_api_key
    elif provider == "volcengine":
        env_key = os.getenv("VOLCENGINE_API_KEY", "") or effective_image_key
        if env_key:
            parameters["volcengine_api_key"] = env_key

    if req.google_api_key:
        parameters["google_api_key"] = req.google_api_key
    elif provider == "google":
        env_key = os.getenv("GOOGLE_API_KEY", "") or effective_image_key
        if env_key:
            parameters["google_api_key"] = env_key

    # 自定义 OpenAI 兼容端点：base_url/model 取请求体无则 settings；key 走 api_key/settings
    if provider not in _BUILTIN_PROVIDERS:
        parameters["image_base_url"] = settings.image_base_url
        parameters["image_model"] = settings.image_model
        if effective_image_key and not req.api_key:
            parameters["api_key"] = effective_image_key

    ctx = SkillContext(
        project_id="",
        db=None,
        llm=gateway,
        parameters=parameters,
    )

    result = await skill.safe_execute(ctx)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return {"success": True, "data": result.data}


@router.get("/providers")
async def list_providers():
    volcengine_key = os.getenv("VOLCENGINE_API_KEY", "")
    google_key = os.getenv("GOOGLE_API_KEY", "")
    from core.secret_resolver import resolve_secret
    from core.settings import get_settings

    settings = get_settings()
    image_key, _source = resolve_secret("image_api_key")
    image_key = settings.image_api_key or image_key

    providers = [
        {
            "name": "volcengine",
            "display_name": "火山方舟",
            "configured": bool(volcengine_key or image_key),
            "description": "火山方舟视觉生成API",
        },
        {
            "name": "google",
            "display_name": "Google AI Studio (Imagen)",
            "configured": bool(google_key or image_key),
            "description": "Google Imagen 3.0 图片生成",
        },
        {
            "name": "fallback",
            "display_name": "默认服务",
            "configured": True,
            "description": "Trae text-to-image 免费服务（不可达时自动离线占位图）",
        },
    ]
    # G-7 收尾：自定义 OpenAI 兼容端点（settings.image_provider 非内置时展示）
    custom_name = settings.image_provider
    if custom_name and custom_name not in _BUILTIN_PROVIDERS:
        providers.append(
            {
                "name": custom_name,
                "display_name": f"自定义端点：{custom_name}",
                "configured": bool(settings.image_base_url and image_key),
                "description": (
                    f"OpenAI 兼容 images/generations（{settings.image_base_url or '未配置 base_url'}，"
                    f"模型 {settings.image_model or '未配置'}）"
                ),
            }
        )
    return {"providers": providers}


@router.put("/config")
async def save_provider_config(config: ProviderConfig):
    lines: list[str] = []

    if _ENV_PATH.exists():
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    keys_to_set: dict[str, str | None] = {
        "VOLCENGINE_API_KEY": config.volcengine_api_key,
        "GOOGLE_API_KEY": config.google_api_key,
    }

    updated_keys: set[str] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        for key, value in keys_to_set.items():
            if stripped.startswith(f"{key}="):
                if value is not None:
                    lines[i] = f"{key}={value}\n"
                else:
                    lines[i] = f"{key}=\n"
                updated_keys.add(key)
                break

    for key, value in keys_to_set.items():
        if key not in updated_keys:
            lines.append(f"{key}={value or ''}\n")

    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)

    if config.volcengine_api_key:
        os.environ["VOLCENGINE_API_KEY"] = config.volcengine_api_key
    if config.google_api_key:
        os.environ["GOOGLE_API_KEY"] = config.google_api_key

    return {
        "success": True,
        "message": "API配置已保存",
        "volcengine_configured": bool(config.volcengine_api_key),
        "google_configured": bool(config.google_api_key),
    }
