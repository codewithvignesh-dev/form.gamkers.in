"""Let admins mark team profiles as leaders for the public landing page.

Revision ID: e5c1a7d93b28
Revises: d3b9f4a60e17
"""
from alembic import op
import sqlalchemy as sa


revision = "e5c1a7d93b28"
down_revision = "d3b9f4a60e17"
branch_labels = None
depends_on = None


def _has_column(table, column):
    return any(item["name"] == column for item in sa.inspect(op.get_bind()).get_columns(table))


def upgrade():
    # Restart-safe: MySQL commits DDL implicitly, so a half-applied run may have the column.
    if not _has_column("team_profile", "is_leader"):
        op.add_column(
            "team_profile",
            sa.Column("is_leader", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade():
    if _has_column("team_profile", "is_leader"):
        op.drop_column("team_profile", "is_leader")
