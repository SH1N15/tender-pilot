"""存储后端抽象（P1-4）：BMP_STORAGE_BACKEND=local|minio 切换。"""

from __future__ import annotations

from core.storage.base import StorageBackend, normalize_key
from core.storage.local import LocalStorageBackend
from core.storage.minio_backend import MinioStorageBackend

_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """按配置返回存储后端单例（local 默认，minio 沿用 BMP_MINIO_* 配置）。"""
    global _backend
    if _backend is not None:
        return _backend
    from core.settings import get_settings

    settings = get_settings()
    backend_name = (settings.storage_backend or "local").lower()
    if backend_name == "minio":
        from core.storage.minio_backend import MinioStorageBackend
        _backend = MinioStorageBackend(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=False,
        )
    else:
        # local 默认：根目录为进程 CWD（与现有 "./projects"、"./uploads" 相对路径一致）
        _backend = LocalStorageBackend(root=".")
    return _backend


def reset_storage() -> None:
    """测试隔离用：清空后端单例。"""
    global _backend
    _backend = None


__all__ = [
    "LocalStorageBackend",
    "MinioStorageBackend",
    "StorageBackend",
    "get_storage",
    "normalize_key",
    "reset_storage",
]
