"""P-A: 文档结构化产物表（实体抽取 + 产物登记）。

表：
- tender_entities: 招投标实体（规则+LLM 双路，带页码证据与待审标记）；
- structured_artifacts: 结构化产物 JSON 登记簿（layout/tables/chunks/entities）。

仅新增表，不动任何现有表/数据；生产 chroma_db 与用户数据零污染。

Revision ID: b8e2f3a4c5d6
Revises: c5d9e1f7a2b4
Create Date: 2026-08-30

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e2f3a4c5d6"
down_revision: Union[str, None] = "c5d9e1f7a2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tender_entities",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("norm", sa.String(length=500), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("conflict", sa.Boolean(), nullable=True),
        sa.Column("review_status", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_tender_entities_project_id", "tender_entities", ["project_id"])
    op.create_index("ix_tender_entities_document_id", "tender_entities", ["document_id"])
    op.create_index("ix_tender_entities_entity_type", "tender_entities", ["entity_type"])

    op.create_table(
        "structured_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("artifact_type", sa.String(length=30), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_structured_artifacts_project_id", "structured_artifacts", ["project_id"])
    op.create_index("ix_structured_artifacts_document_id", "structured_artifacts", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_structured_artifacts_document_id", table_name="structured_artifacts")
    op.drop_index("ix_structured_artifacts_project_id", table_name="structured_artifacts")
    op.drop_table("structured_artifacts")
    op.drop_index("ix_tender_entities_entity_type", table_name="tender_entities")
    op.drop_index("ix_tender_entities_document_id", table_name="tender_entities")
    op.drop_index("ix_tender_entities_project_id", table_name="tender_entities")
    op.drop_table("tender_entities")
