"""MinIO / S3 兼容对象存储后端（P1-4）。

注意：本机无 Docker，无法真实运行 MinIO——本模块只保证 mock 单测通过，
真实环境验收待有 Docker/MinIO 时进行（见 docs/handover/04）。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from core.storage.base import StorageBackend, normalize_key

logger = logging.getLogger(__name__)


class MinioStorageBackend(StorageBackend):
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str = "bidmaster",
        secure: bool = False,
    ):
        from minio import Minio  # 延迟导入，未安装时不影响 local 后端

        self.bucket = bucket
        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
        except Exception as e:  # noqa: BLE001
            logger.warning("MinIO bucket 检查/创建失败（忽略，写入时报错）: %s", e)

    def save(self, key: str, data: bytes) -> str:
        key = normalize_key(key)
        self.client.put_object(self.bucket, key, io.BytesIO(data), length=len(data))
        return key

    def load(self, key: str) -> bytes:
        key = normalize_key(key)
        response = self.client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        key = normalize_key(key)
        try:
            self.client.stat_object(self.bucket, key)
            return True
        except S3Error as e:
            if e.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise

    def delete(self, key: str) -> None:
        self.client.remove_object(self.bucket, normalize_key(key))

    def store_local_file(self, local_path: str | Path, key: str) -> str:
        from minio.commonconfig import CopySource  # noqa: F401  # 保留导入位（当前按字节上传）

        return self.save(key, Path(local_path).read_bytes())


__all__ = ["MinioStorageBackend"]
