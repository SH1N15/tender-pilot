"""存储后端抽象基类（P1-4）。

约定：
- key 统一使用正斜杠相对路径，如 `projects/{project_id}/{filename}`、`uploads/formatted/{filename}`；
- local 后端把 key 映射到仓库根目录下的同一相对路径，目录结构与文件名与现状完全一致（零迁移风险）；
- minio 后端把 key 映射到 bucket 内的对象路径。
"""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    """对象存储抽象：save / load / exists / delete / local_path。"""

    @abstractmethod
    def save(self, key: str, data: bytes) -> str:
        """写入对象，返回 key。"""

    @abstractmethod
    def load(self, key: str) -> bytes:
        """读取对象内容。key 不存在时抛 FileNotFoundError。"""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    def local_path(self, key: str) -> Path | None:
        """若后端提供本地文件路径则返回，否则 None（默认 None）。"""
        return None

    def store_local_file(self, local_path: str | Path, key: str) -> str:
        """把一个已生成的本地文件（如 DOCX/PDF 导出产物）推入存储后端。"""
        data = Path(local_path).read_bytes()
        return self.save(key, data)

    def ensure_local(self, key_or_path: str) -> str:
        """返回可被本地文件工具（解析器/转换器）直接使用的本地路径。

        - 本地后端：直接返回映射路径；
        - 远端后端：下载到临时文件（调用方负责清理）。
        """
        p = self.local_path(key_or_path)
        if p is not None and p.exists():
            return str(p)
        data = self.load(key_or_path)
        suffix = Path(key_or_path).suffix
        fd, tmp_name = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return tmp_name


def normalize_key(key: str) -> str:
    """归一化 key：统一正斜杠、去掉 ./ 前缀。"""
    return str(key).replace("\\", "/").lstrip("./").lstrip("/")


__all__ = ["StorageBackend", "normalize_key"]
