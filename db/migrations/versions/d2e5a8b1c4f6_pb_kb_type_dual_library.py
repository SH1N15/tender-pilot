"""P-B 双库治理：knowledge_bases 增加 kb_type / review_status / valid_until

Revision ID: d2e5a8b1c4f6
Revises: b8e2f3a4c5d6
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e5a8b1c4f6"
down_revision: str | None = "b8e2f3a4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column("kb_type", sa.String(length=20), nullable=False, server_default="enterprise"),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("review_status", sa.String(length=20), nullable=False, server_default="draft"),
    )
    op.add_column("knowledge_bases", sa.Column("valid_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_bases", "valid_until")
    op.drop_column("knowledge_bases", "review_status")
    op.drop_column("knowledge_bases", "kb_type")
