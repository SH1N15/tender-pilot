from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.settings import get_settings

logger = logging.getLogger(__name__)

_engine = None
_async_session_factory = None
_db_ready = False


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        database_url = settings.get_database_url()
        logger.info(
            f"数据库类型: {settings.db_type}, "
            f"连接串: {database_url.split('@')[-1] if '@' in database_url else database_url}"
        )
        engine_kwargs = {
            "echo": settings.debug,
            "pool_size": 20,
            "max_overflow": 10,
        }
        if settings.db_type == "mysql":
            engine_kwargs["pool_recycle"] = 3600
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["connect_args"] = {"charset": "utf8mb4"}
        _engine = create_async_engine(database_url, **engine_kwargs)
    return _engine


def async_session() -> async_sessionmaker[AsyncSession]:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


@asynccontextmanager
async def db_session():
    """供 MCP/A2A 等非 HTTP 调用复用的数据库会话（自动 commit/rollback）。"""
    factory = async_session()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncSession:
    if not _db_ready:
        raise HTTPException(status_code=503, detail="数据库暂不可用，请检查数据库服务是否已启动")
    session_factory = async_session()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def is_db_ready() -> bool:
    return _db_ready


async def seed_default_admin():
    """数据库可用时确保默认管理员存在（初始化账号）。"""
    import hashlib

    from services.models import User

    email = os.getenv("BMP_ADMIN_EMAIL", "admin@bidmaster.pro")
    password = os.getenv("BMP_ADMIN_PASSWORD", "admin123")
    name = os.getenv("BMP_ADMIN_NAME", "平台管理员")
    hashed = hashlib.sha256(password.encode("utf-8")).hexdigest()

    async with async_session()() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            db.add(User(email=email, name=name, role="admin", password_hash=hashed))
            await db.commit()
            logger.info("已创建默认管理员: %s", email)
        else:
            if not user.password_hash:
                user.password_hash = hashed
                await db.commit()


def _alembic_config():
    """构建 Alembic Config（绝对路径，避免依赖启动 cwd）。"""
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "db" / "migrations"))
    return cfg


async def _has_alembic_version_table() -> bool:
    """检测当前库是否存在 alembic_version 表。"""
    from sqlalchemy import inspect

    async with get_engine().connect() as conn:
        return await conn.run_sync(lambda c: inspect(c).has_table("alembic_version"))


async def ensure_migrations() -> None:
    """启动时自动对齐迁移版本（roadmap P0-3）。

    - 无 `alembic_version` 表（存量库/旧库）：`stamp head`，免重建；
    - 已有：`upgrade head` 增量升级；
    - 失败只告警不阻断启动（与 init_db 同语义）。
    """
    try:
        from alembic import command

        cfg = _alembic_config()
        if await _has_alembic_version_table():
            await asyncio.to_thread(command.upgrade, cfg, "head")
            logger.info("Alembic 迁移已对齐到 head")
        else:
            await asyncio.to_thread(command.stamp, cfg, "head")
            logger.info("存量库已 stamp 到 Alembic head")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Alembic 迁移失败（不阻断启动）: {e}")


async def init_db():
    global _db_ready
    from services.models import Base

    try:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _db_ready = True
        logger.info("数据库初始化成功")
        await seed_default_admin()
        await ensure_migrations()
    except Exception as e:
        logger.warning(f"数据库不可用，应用将以降级模式启动: {e}")
        _db_ready = False


async def close_db():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
