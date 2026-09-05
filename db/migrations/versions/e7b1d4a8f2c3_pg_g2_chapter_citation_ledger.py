"""P-G G-2：chapters 表新增 citation_ledger JSON 列（引用对照表落库+前端联动）。

Revision ID: e7b1d4a8f2c3
Revises: d2e5a8b1c4f6
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b1d4a8f2c3"
down_revision: str | None = "d2e5a8b1c4f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chapters", sa.Column("citation_ledger", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("chapters", "citation_ledger")
