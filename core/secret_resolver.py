"""Secret 解析器（roadmap P0-2）。

读取优先级：**进程环境变量 > Windows 凭据管理器（keyring）> `.env` 文件 > missing**。

- keyring 为可选依赖：未安装 / 无条目 / 访问异常时静默跳过（视为不存在）；
- 绝不打印、不落盘任何明文 secret；掩码规则见 `mask_secret`（只露末 4 位）；
- `.env` 现有内容不改不删（写入仍走 services.env_store）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "bidmaster-pro"

# 受管理的 secret 字段（Settings 上的字段名）→ 凭据管理器条目名（即环境变量名）
SECRET_FIELDS: dict[str, str] = {
    "llm_api_key": "BMP_LLM_API_KEY",
    "embedding_api_key": "BMP_EMBEDDING_API_KEY",
    "ocr_api_key": "BMP_OCR_API_KEY",
    "image_api_key": "BMP_IMAGE_API_KEY",
    "minio_secret_key": "BMP_MINIO_SECRET_KEY",
    "mysql_password": "BMP_MYSQL_PASSWORD",
}


class KeyringUnavailableError(Exception):
    """keyring 不可用（未安装/后端不可用）。内部使用，静默吞掉。"""


def _get_keyring():
    """返回 keyring 模块，未安装则返回 None（静默降级）。"""
    try:
        import keyring  # noqa: SIM401  # 可选依赖
    except Exception:  # noqa: BLE001
        return None
    return keyring


def read_keyring_secret(entry_name: str) -> str | None:
    """从 Windows 凭据管理器读取 secret；keyring 不可用/无条目时返回 None（不报错）。"""
    kr = _get_keyring()
    if kr is None:
        return None
    try:
        value = kr.get_password(KEYRING_SERVICE, entry_name)
    except Exception as e:  # noqa: BLE001
        logger.debug("keyring 读取 %s 失败（忽略）: %s", entry_name, e)
        return None
    return value or None


def write_keyring_secret(entry_name: str, value: str) -> bool:
    """写入凭据管理器（供平台设置页使用）；失败返回 False，不抛异常。"""
    kr = _get_keyring()
    if kr is None:
        return False
    try:
        kr.set_password(KEYRING_SERVICE, entry_name, value)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("keyring 写入 %s 失败: %s", entry_name, e)
        return False


def delete_keyring_secret(entry_name: str) -> bool:
    kr = _get_keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(KEYRING_SERVICE, entry_name)
        return True
    except Exception:  # noqa: BLE001
        return False


def read_envfile_secret(entry_name: str) -> str | None:
    """从 .env 文件读取（不经 pydantic，独立于进程环境）。"""
    try:
        from services.env_store import read_env

        return read_env().get(entry_name) or None
    except Exception:  # noqa: BLE001
        return None


def resolve_secret(field_name: str) -> tuple[str | None, str]:
    """解析单个 secret，返回 (值或None, 来源)。来源 ∈ {env, keyring, envfile, missing}。"""
    entry_name = SECRET_FIELDS.get(field_name, f"BMP_{field_name.upper()}")
    value = os.environ.get(entry_name)
    if value:
        return value, "env"
    value = read_keyring_secret(entry_name)
    if value:
        return value, "keyring"
    value = read_envfile_secret(entry_name)
    if value:
        return value, "envfile"
    return None, "missing"


def mask_secret(value: str | None) -> str:
    """掩码：只露末 4 位；空值显示 (未配置)。"""
    if not value:
        return "(未配置)"
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


class KeyringSettingsSource:
    """pydantic-settings 自定义 source：优先级位于进程环境变量之后、.env 之前。"""

    def __init__(self, fields: dict[str, str] | None = None):
        self._fields = fields or SECRET_FIELDS

    def __call__(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for field_name in self._fields:
            # 进程环境变量已由 EnvSettingsSource 覆盖，这里只补 keyring
            entry_name = self._fields[field_name]
            if os.environ.get(entry_name):
                continue
            value = read_keyring_secret(entry_name)
            if value:
                data[field_name] = value
        return data
