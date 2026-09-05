"""本地文件存储后端（现状默认，行为完全不变）。"""

from __future__ import annotations

from pathlib import Path

from core.storage.base import StorageBackend, normalize_key


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        return self.root / normalize_key(key)

    def save(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def load(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(f"对象不存在: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def local_path(self, key: str) -> Path | None:
        return self._path(key)


__all__ = ["LocalStorageBackend"]
