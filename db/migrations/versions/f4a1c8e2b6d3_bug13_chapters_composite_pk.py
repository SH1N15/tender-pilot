"""BUG-13: chapters 主键改为复合 (project_id, id)

大纲物化使用节点号（"1"/"1.1"）作为 chapters.id，该编号只在项目内唯一。
原全局单列主键 chapters_pkey 导致第二个项目物化相同节点号时
UniqueViolationError（跨项目串写/大纲任务失败）。

注意：
- Alembic autogenerate 不检测主键变更，本迁移为手工编写（风格对齐 autogenerate）。
- SQLite 不支持约束 ALTER，走 batch 重建表；PostgreSQL 原地 swap 主键。
- 现有库数据不受影响：串写是 UPDATE 行为，不会产生跨项目重复 id，
  因此重建主键不会因数据冲突失败。
- downgrade 回退到全局单列主键，若届时已存在跨项目重复 id 需先人工去重。

Revision ID: f4a1c8e2b6d3
Revises: af8aa9790e66
Create Date: 2026-08-29

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4a1c8e2b6d3'
down_revision: Union[str, None] = 'af8aa9790e66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _swap_primary_key(columns: list[str]) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite：batch 重建表（复制数据 + 约束/索引）。
        # 内联 PRIMARY KEY 反射出来是匿名约束，需提供命名约定使其可按名删除。
        naming = {
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "chapters_pkey",
        }
        with op.batch_alter_table(
            "chapters", recreate="always", naming_convention=naming
        ) as batch_op:
            batch_op.drop_index("ix_chapters_project_id")
            batch_op.drop_constraint("chapters_pkey", type_="primary")
            batch_op.create_primary_key("chapters_pkey", columns)
            batch_op.create_index(
                "ix_chapters_project_id", ["project_id"], unique=False
            )
    else:
        op.drop_constraint("chapters_pkey", "chapters", type_="primary")
        op.create_primary_key("chapters_pkey", "chapters", columns)


def upgrade() -> None:
    _swap_primary_key(["project_id", "id"])


def downgrade() -> None:
    _swap_primary_key(["id"])
