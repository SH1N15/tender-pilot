"""P-D1: LangGraph 主编排图持久 checkpoint 表（自研 JSONB saver）。

表：
- graph_checkpoints: thread_id + checkpoint_ns + checkpoint_id 唯一；
  checkpoint / metadata 存 JSONB（type+b64 封装，JsonPlusSerializer 格式）。
- graph_checkpoint_writes: interrupt 挂起时的 pending writes。

仅新增表，不动任何现有表/数据；生产 chroma_db 与用户数据零污染。

Revision ID: c5d9e1f7a2b4
Revises: f4a1c8e2b6d3
Create Date: 2026-08-30

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c5d9e1f7a2b4'
down_revision: Union[str, None] = 'f4a1c8e2b6d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB_OR_JSON = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "graph_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("parent_checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column("checkpoint", JSONB_OR_JSON, nullable=False),
        sa.Column("metadata", JSONB_OR_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("thread_id", "checkpoint_ns", "checkpoint_id", name="uq_graph_checkpoint"),
    )
    op.create_index("ix_graph_checkpoints_thread", "graph_checkpoints", ["thread_id"])

    op.create_table(
        "graph_checkpoint_writes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=True),
        sa.Column("blob", JSONB_OR_JSON, nullable=True),
        sa.UniqueConstraint(
            "thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx",
            name="uq_graph_checkpoint_write",
        ),
    )
    op.create_index("ix_graph_checkpoint_writes_thread", "graph_checkpoint_writes", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_graph_checkpoint_writes_thread", table_name="graph_checkpoint_writes")
    op.drop_table("graph_checkpoint_writes")
    op.drop_index("ix_graph_checkpoints_thread", table_name="graph_checkpoints")
    op.drop_table("graph_checkpoints")
