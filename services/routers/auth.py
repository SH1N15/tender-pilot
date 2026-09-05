from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timedelta

import bcrypt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from services.models import AuthSession, RBACRole, RBACUserRole, User

router = APIRouter()

_sessions: dict[str, dict] = {}

SESSION_TTL = 86400 * 7

logger = logging.getLogger(__name__)

DEV_LOGIN_EMAIL = os.getenv("BMP_DEV_LOGIN_EMAIL", "admin@dev.local")
DEV_LOGIN_PASSWORD = os.getenv("BMP_DEV_LOGIN_PASSWORD", "dev123")
DEV_LOGIN_ENABLED = os.getenv("BMP_DEV_LOGIN", "true").lower() in ("1", "true", "yes")


def _dev_user_info() -> dict:
    return {
        "id": "dev-admin-0000",
        "email": DEV_LOGIN_EMAIL,
        "name": "开发管理员",
        "role": "admin",
        "avatar": None,
        "roles": [{"id": "dev-admin", "name": "admin", "display_name": "管理员"}],
    }


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenInfo(BaseModel):
    token: str
    user_id: str
    email: str
    name: str
    role: str
    avatar: str | None = None


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _hash_token(token: str) -> str:
    """会话表存 sha256(token) 而非明文，防拖库冒用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_legacy_sha256_hash(stored: str) -> bool:
    return len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower())


def _verify_password(password: str, stored: str) -> bool:
    try:
        if _is_legacy_sha256_hash(stored):
            return hashlib.sha256(password.encode("utf-8")).hexdigest() == stored.lower()
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except Exception:
        return False


async def _create_session(db, token: str, user_id: str, email: str, name: str, role: str) -> None:
    """持久化会话（调用方负责 commit）。"""
    db.add(
        AuthSession(
            token_hash=_hash_token(token),
            user_id=user_id,
            email=email,
            name=name,
            role=role,
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(days=7),
        )
    )


async def _cleanup_expired_sessions(db) -> None:
    """删除已过期的持久会话行（调用方负责 commit）。"""
    result = await db.execute(select(AuthSession).where(AuthSession.expires_at < datetime.now()))
    for session_row in result.scalars().all():
        await db.delete(session_row)


def _cleanup_sessions():
    now = time.time()
    expired = [k for k, v in _sessions.items() if now - v["created_at"] > SESSION_TTL]
    for k in expired:
        del _sessions[k]


def _make_session(user_id: str, email: str, name: str, role: str) -> str:
    _cleanup_sessions()
    token = secrets.token_hex(32)
    _sessions[token] = {
        "user_id": user_id,
        "email": email,
        "name": name,
        "role": role,
        "created_at": time.time(),
    }
    return token


def _memory_session_to_dict(session: dict) -> dict:
    return {
        "user_id": session["user_id"],
        "email": session["email"],
        "name": session["name"],
        "role": session["role"],
        "created_at": session["created_at"],
    }


def verify_token(token: str) -> dict | None:
    """同步兼容入口：无事件循环时直接走 DB，否则交给运行中的循环。"""
    if not token:
        return None
    from services.database import async_session, is_db_ready

    if is_db_ready():
        try:
            return _run_coro_sync(_verify_token_db(async_session(), token))
        except Exception as exc:
            logger.warning("持久会话校验异常，回退内存会话: %s", exc)
    return _verify_token_memory(token)


async def verify_token_async(token: str) -> dict | None:
    """异步上下文（FastAPI 路由/中间件）使用：与 verify_token 返回结构一致。"""
    if not token:
        return None
    from services.database import async_session, is_db_ready

    if is_db_ready():
        try:
            return await _verify_token_db(async_session(), token)
        except Exception as exc:
            logger.warning("持久会话校验异常，回退内存会话: %s", exc)
    return _verify_token_memory(token)


def _run_coro_sync(coro):
    """在同步上下文中执行协程；若已有事件循环在跑，则放到独立线程的循环中执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def _verify_token_memory(token: str) -> dict | None:
    _cleanup_sessions()
    session = _sessions.get(token)
    if not session:
        return None
    if time.time() - session["created_at"] > SESSION_TTL:
        del _sessions[token]
        return None
    return _memory_session_to_dict(session)


async def _verify_token_db(session_factory, token: str) -> dict | None:
    async with session_factory() as db:
        result = await db.execute(select(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
        session_row = result.scalar_one_or_none()
        if not session_row:
            return None
        if session_row.expires_at < datetime.now():
            await db.delete(session_row)
            await db.commit()
            return None
        user_result = await db.execute(select(User).where(User.id == session_row.user_id))
        user = user_result.scalar_one_or_none()
        if user:
            email, name, role = user.email, user.name, user.role
            created_at = session_row.created_at
        else:
            email, name, role = session_row.email, session_row.name, session_row.role
            created_at = session_row.created_at
        return {
            "user_id": session_row.user_id,
            "email": email,
            "name": name,
            "role": role,
            "created_at": created_at.timestamp(),
        }


# ─────────────────────────────────────────────────────────────
# 登录：开发账号（无数据库 / DB 不可用） > 数据库账号
# ─────────────────────────────────────────────────────────────


@router.post("/login")
async def login(data: LoginRequest):
    if DEV_LOGIN_ENABLED and data.email == DEV_LOGIN_EMAIL and data.password == DEV_LOGIN_PASSWORD:
        token = "dev-" + secrets.token_hex(30)
        _cleanup_sessions()
        _sessions[token] = {
            "user_id": "dev-admin-0000",
            "email": DEV_LOGIN_EMAIL,
            "name": "开发管理员",
            "role": "admin",
            "created_at": time.time(),
        }
        return {"token": token, "user": _dev_user_info()}

    from services.database import async_session, is_db_ready

    # 数据库不可用：开发账号已在上方优先匹配；到这里说明是不匹配的开发账号或其他账号
    if not is_db_ready():
        if DEV_LOGIN_ENABLED:
            raise HTTPException(status_code=401, detail="数据库不可用，请使用开发模式账号 admin@dev.local / dev123")
        raise HTTPException(status_code=503, detail="数据库暂不可用")

    # 数据库可用：尝试正常数据库登录
    try:
        async with async_session()() as db:
            result = await db.execute(select(User).where(User.email == data.email))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=401, detail="邮箱或密码错误")
            if not user.password_hash:
                raise HTTPException(status_code=401, detail="该账户未设置密码，请联系管理员")
            if not _verify_password(data.password, user.password_hash):
                raise HTTPException(status_code=401, detail="邮箱或密码错误")
            if _is_legacy_sha256_hash(user.password_hash):
                user.password_hash = _hash_password(data.password)
            await _cleanup_expired_sessions(db)
            token = secrets.token_hex(32)
            await _create_session(db, token, str(user.id), user.email, user.name, user.role)
            await db.commit()
            ur_result = await db.execute(select(RBACUserRole.role_id).where(RBACUserRole.user_id == user.id))
            role_ids = [row[0] for row in ur_result.all()]
            roles = []
            if role_ids:
                role_result = await db.execute(select(RBACRole).where(RBACRole.id.in_(role_ids)))
                roles = [
                    {"id": str(r.id), "name": r.name, "display_name": r.display_name}
                    for r in role_result.scalars().all()
                ]
            return {
                "token": token,
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "name": user.name,
                    "role": user.role,
                    "avatar": user.avatar,
                    "roles": roles,
                },
            }
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("数据库登录异常: %s", exc)
        raise HTTPException(status_code=503, detail="数据库暂不可用")


@router.get("/me")
async def get_current_user_info(token: str):
    session = await verify_token_async(token)
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    if session.get("user_id") == "dev-admin-0000":
        return _dev_user_info()

    from services.database import async_session, is_db_ready

    if not is_db_ready():
        raise HTTPException(status_code=503, detail="数据库暂不可用")
    try:
        async with async_session()() as db:
            result = await db.execute(select(User).where(User.id == session["user_id"]))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=401, detail="用户不存在")
            ur_result = await db.execute(select(RBACUserRole.role_id).where(RBACUserRole.user_id == user.id))
            role_ids = [row[0] for row in ur_result.all()]
            roles = []
            if role_ids:
                role_result = await db.execute(select(RBACRole).where(RBACRole.id.in_(role_ids)))
                roles = [
                    {"id": str(r.id), "name": r.name, "display_name": r.display_name}
                    for r in role_result.scalars().all()
                ]
            return {
                "id": str(user.id),
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "avatar": user.avatar,
                "roles": roles,
            }
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("数据库 /me 查询异常: %s", exc)
        raise HTTPException(status_code=503, detail="数据库暂不可用")


@router.post("/logout")
async def logout(token: str = ""):
    if token:
        from services.database import async_session, is_db_ready

        if is_db_ready():
            try:
                async with async_session()() as db:
                    result = await db.execute(
                        select(AuthSession).where(AuthSession.token_hash == _hash_token(token))
                    )
                    for row in result.scalars().all():
                        await db.delete(row)
                    await db.commit()
                    return {"success": True}
            except Exception as exc:
                logger.warning("持久会话注销异常，回退内存会话: %s", exc)
        _sessions.pop(token, None)
    return {"success": True}


@router.put("/change-password")
async def change_password(token: str, old_password: str, new_password: str):
    session = await verify_token_async(token)
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    if session.get("user_id") == "dev-admin-0000":
        raise HTTPException(status_code=400, detail="开发模式账号不支持修改密码")

    from services.database import async_session, is_db_ready

    if not is_db_ready():
        raise HTTPException(status_code=503, detail="数据库暂不可用")
    try:
        async with async_session()() as db:
            result = await db.execute(select(User).where(User.id == session["user_id"]))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=401, detail="用户不存在")
            if not _verify_password(old_password, user.password_hash):
                raise HTTPException(status_code=400, detail="原密码错误")
            user.password_hash = _hash_password(new_password)
            sess_result = await db.execute(select(AuthSession).where(AuthSession.user_id == str(user.id)))
            for row in sess_result.scalars().all():
                await db.delete(row)
            await db.commit()
            return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("数据库改密异常: %s", exc)
        raise HTTPException(status_code=503, detail="数据库暂不可用")
