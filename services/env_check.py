"""环境/依赖自检（vNext）。

用法：python -m services.env_check
只输出状态与掩码信息，绝不打印任何 secret（API Key / token / 密码）。
"""

from __future__ import annotations

import asyncio
import platform
import subprocess
import sys
from pathlib import Path

from core.settings import get_settings


def _detect_memurai_service() -> bool:
    """Windows 下检测 Memurai 服务是否运行（官方 Redis 兼容服务）。"""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["sc", "query", "Memurai"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return out.returncode == 0 and "RUNNING" in out.stdout.upper()
    except Exception:
        return False


async def _check_db() -> dict:
    settings = get_settings()
    url = settings.get_database_url()
    db_name = url.split("@")[-1].split("/")[-1] if "@" in url else url
    try:
        import asyncpg

        conn = await asyncio.wait_for(
            asyncpg.connect(dsn=url.replace("postgresql+asyncpg://", "postgresql://"), timeout=3),
            timeout=5,
        )
        await conn.close()
        return {"status": "ok", "name": "PostgreSQL/MySQL", "database": db_name}
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "name": "PostgreSQL/MySQL", "database": db_name, "error": str(e)[:120]}


async def _check_redis() -> dict:
    settings = get_settings()
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        pong = await asyncio.wait_for(client.ping(), timeout=5)
        await client.aclose()
        backend = "Memurai" if _detect_memurai_service() else "redis"
        return {
            "status": "ok" if pong else "error",
            "name": "Redis",
            "backend": backend,
            "url_host": settings.redis_url.split("@")[-1].split("/")[0],
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "name": "Redis", "error": str(e)[:120]}


def _check_llm() -> dict:
    settings = get_settings()
    return {
        "status": "configured" if settings.llm_api_key else "not_configured",
        "name": "LLM",
        "model": settings.llm_default_model,
        "api_base": settings.llm_api_base,
        "api_key_set": bool(settings.llm_api_key),
    }


def _check_ocr() -> dict:
    settings = get_settings()
    return {
        "status": "configured" if settings.ocr_mode in ("cloud", "selfhosted", "mock") else "off",
        "name": "MinerU OCR",
        "mode": settings.ocr_mode,
        "endpoint": settings.ocr_endpoint,
        "api_key_set": bool(settings.ocr_api_key),
    }


def _check_embedding() -> dict:
    settings = get_settings()
    return {
        "status": "configured" if settings.embedding_mode else "not_configured",
        "name": "Embedding",
        "mode": settings.embedding_mode,
        "model": settings.embedding_model,
        "api_key_set": bool(settings.embedding_api_key),
    }


async def _check_mcp() -> dict:
    settings = get_settings()
    try:
        from services.mcp.server import get_mcp_capabilities

        caps = await get_mcp_capabilities()
        return {
            "status": "ok" if caps["enabled"] and settings.mcp_enabled else "disabled",
            "name": "MCP",
            "tools_count": caps["tools_count"],
            "resources_count": caps["resources_count"],
            "transports": caps["transports"],
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "name": "MCP", "error": str(e)[:120]}


def _check_a2a() -> dict:
    settings = get_settings()
    from services.a2a_server import A2A_SDK_VERSION

    return {
        "status": "ok" if settings.a2a_enabled else "disabled",
        "name": "A2A",
        "sdk": A2A_SDK_VERSION,
        "agent_card_url": f"{(settings.public_base_url or f'http://localhost:{settings.port}').rstrip('/')}/.well-known/agent-card.json",
    }


def _check_agui() -> dict:
    settings = get_settings()
    from services.agui import AGUI_SDK_VERSION

    return {
        "status": "ok" if settings.agui_enabled else "disabled",
        "name": "AG-UI",
        "sdk": AGUI_SDK_VERSION,
        "run_url": f"{(settings.public_base_url or f'http://localhost:{settings.port}').rstrip('/')}/api/agui/run",
    }


def _check_tracing() -> dict:
    settings = get_settings()
    return {
        "status": "enabled" if settings.trace_enabled else "disabled",
        "name": "Tracing",
        "trace_dir": settings.trace_dir,
        "otlp_endpoint_set": bool(settings.otlp_endpoint),
    }


def _check_secrets() -> dict:
    """逐个 secret 报告来源（env/keyring/envfile/missing）与掩码（只露末 4 位）。"""
    from core.secret_resolver import SECRET_FIELDS, mask_secret, resolve_secret

    secrets = {}
    for field_name in SECRET_FIELDS:
        value, source = resolve_secret(field_name)
        secrets[field_name] = {"source": source, "masked": mask_secret(value) if value else None}
    return {"name": "Secrets来源", "status": "ok", "secrets": secrets}


async def run_checks() -> list[dict]:
    checks = [
        {
            "status": "ok",
            "name": "Python",
            "version": sys.version.split()[0],
            "platform": platform.platform(),
            "venv": str(Path(sys.prefix)),
        },
        {"name": "数据库", **(await _check_db())},
        {"name": "Redis", **(await _check_redis())},
        _check_llm(),
        _check_embedding(),
        _check_ocr(),
        {"name": "MCP", **(await _check_mcp())},
        _check_a2a(),
        _check_agui(),
        _check_tracing(),
        _check_secrets(),
    ]
    return checks


def _mask_url(url: str) -> str:
    return url


def main() -> None:
    asyncio.run(_print())


async def _print() -> None:
    checks = await run_checks()
    width = 22
    print("=" * 76)
    print("投标智航 / TenderPilot vNext 环境自检")
    print("=" * 76)
    for check in checks:
        status = check.get("status", "?")
        name = check.get("name", "")
        line = f"{name:<{width}}{status:<14}"
        extras = []
        for key in ("version", "model", "mode", "tools_count", "resources_count", "api_base", "database", "url_host"):
            if key in check and check[key] not in (None, ""):
                extras.append(f"{key}={check[key]}")
        if check.get("api_key_set"):
            extras.append("api_key=已配置(掩码)")
        elif "api_key_set" in check:
            extras.append("api_key=未配置")
        if check.get("error"):
            extras.append(f"error={check['error']}")
        print(line + "  " + "  ".join(extras))
        for secret_name, info in (check.get("secrets") or {}).items():
            src = info.get("source", "missing")
            masked = info.get("masked") or "(未配置)"
            print(f"{'':<{width}}  - {secret_name:<20} 来源={src:<8} {masked}")
    print("=" * 76)
    print("提示：所有 secret 均已掩码；LLM/OCR 连接测试请到平台设置页或调用 /api/llm/test、/api/ocr/test。")


if __name__ == "__main__":
    main()
