"""本地 .env 原子读写工具（vNext）。

- 原子写入：写临时文件后 os.replace，避免写一半损坏；
- Windows 上尽量收紧 ACL（其他用户读权限移除，尽力而为，失败不阻塞）；
- 绝不把 secret 打印到日志/响应。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.environ.get("BIDMASTER_ENV_FILE", str(_REPO_ROOT / ".env")))
# BUG-12 诊断插桩：.env 写入审计日志（不落明文 key/value，失败不阻断主流程）
AUDIT_LOG_PATH = _REPO_ROOT / ".dev" / "env_writes.log"


def _mask_value(value: str) -> str:
    """值掩码：仅记录长度与尾 4 位，绝不落明文。"""
    if not value:
        return "len=0"
    return f"len={len(value)},tail={value[-4:]}"


def _audit_env_write(updates: dict[str, str], deletes: list[str]) -> None:
    """每次真实写盘前向 .dev/env_writes.log 追加一行审计 JSON。

    记录：时间、调用方（traceback 倒数第 3-5 帧）、变更键、值掩码。
    任何失败均静默（try/except），绝不阻断 .env 主流程。
    """
    try:
        frames = traceback.extract_stack()[:-2]  # 去掉 _audit_env_write / write_env_atomic 自身
        caller_frames = [f"{f.filename}:{f.lineno}:{f.name}" for f in frames[-3:]]
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "callers": caller_frames,
            "changed_keys": {
                "updated": {k: _mask_value(v) for k, v in updates.items()},
                "deleted": list(deletes),
            },
        }
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug("写入 .env 审计日志失败（忽略）: %s", e)


def get_env_path() -> Path:
    return ENV_PATH


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 .env 失败: %s", e)
    return env


def write_env_atomic(updates: dict[str, str] | None = None, deletes: list[str] | None = None) -> dict[str, str]:
    """原子更新 .env；返回更新后的完整 env（调用方需自行掩码后返回给前端）。"""
    updates = updates or {}
    deletes = deletes or []
    env = read_env()
    for key in deletes:
        env.pop(key, None)
    for key, value in updates.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    _audit_env_write(
        {k: v for k, v in updates.items() if v is not None},
        deletes + [k for k, v in updates.items() if v is None],
    )
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(ENV_PATH.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            for key in sorted(env):
                f.write(f"{key}={env[key]}\n")
        os.replace(tmp_name, ENV_PATH)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _tighten_permissions()
    return env


def _tighten_permissions() -> None:
    """Windows：尽力移除其他用户的读权限；失败仅告警。"""
    try:
        import ntsecuritycon  # type: ignore
        import win32api  # type: ignore
        import win32security  # type: ignore

        path = str(ENV_PATH)
        user_sid, _, _ = win32security.LookupAccountName("", win32api.GetUserName())
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE,
            user_sid,
        )
        win32security.SetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION, dacl)
    except Exception as e:  # noqa: BLE001
        logger.debug("收紧 .env 权限失败（忽略）: %s", e)
