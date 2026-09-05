"""迁移脚本：把 ./projects、./uploads 现有文件推送到 MinIO（幂等，支持 --dry-run）。

用法：
    python scripts/migrate_to_minio.py [--dry-run] [--source ./projects] [--source ./uploads]

- 目标 MinIO 参数沿用环境变量 / .env：BMP_MINIO_ENDPOINT / BMP_MINIO_ACCESS_KEY /
  BMP_MINIO_SECRET_KEY / BMP_MINIO_BUCKET；
- 幂等：对象已存在（同 key）时跳过；
- 仅本地扫描上传，不删除本地文件（回滚 = 本地目录仍在）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def iter_files(source: Path):
    for p in sorted(source.rglob("*")):
        if p.is_file():
            yield p


def main() -> int:
    parser = argparse.ArgumentParser(description="把本地 projects/uploads 文件迁移到 MinIO")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不上传")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="要迁移的目录（可多次出现），默认 ./projects 和 ./uploads",
    )
    args = parser.parse_args()

    sources = [Path(s) for s in (args.source or ["./projects", "./uploads"])]
    sources = [s if s.is_absolute() else (_REPO_ROOT / s) for s in sources]

    from core.storage import LocalStorageBackend, MinioStorageBackend, get_storage, normalize_key

    target = get_storage()
    if isinstance(target, LocalStorageBackend):
        print("[!] 当前存储后端是 local（BMP_STORAGE_BACKEND != minio），迁移无意义，退出。")
        return 2

    assert isinstance(target, MinioStorageBackend)
    total = skipped = uploaded = failed = 0
    for src in sources:
        if not src.exists():
            print(f"[i] 跳过不存在的目录: {src}")
            continue
        for f in iter_files(src):
            total += 1
            key = normalize_key(f.relative_to(_REPO_ROOT))
            if args.dry_run:
                print(f"[dry-run] 将上传 {key}")
                continue
            try:
                if target.exists(key):
                    skipped += 1
                    continue
                target.store_local_file(f, key)
                uploaded += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"[x] 上传失败 {key}: {e}")

    print(f"\n完成: 总计 {total}，已上传 {uploaded}，已存在跳过 {skipped}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
