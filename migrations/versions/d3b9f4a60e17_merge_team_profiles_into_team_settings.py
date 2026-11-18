"""Merge team profiles into portal team management and add custom roles.

The upgrade is deliberately restart-safe. MySQL commits most DDL statements
implicitly, so a failed upgrade can leave some schema changes in place while
Alembic still points at the previous revision.

Revision ID: d3b9f4a60e17
Revises: b7e3c9a15d42
"""
from alembic import op
import sqlalchemy as sa


revision = "d3b9f4a60e17"
down_revision = "b7e3c9a15d42"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table, column):
    return any(item["name"] == column for item in _inspector().get_columns(table))


def _has_fk(table, name):
    return any(item.get("name") == name for item in _inspector().get_foreign_keys(table))


def _has_unique(table, name):
    return any(item.get("name") == name for item in _inspector().get_unique_constraints(table))


def _has_index(table, name):
    return any(item.get("name") == name for item in _inspector().get_indexes(table))


def upgrade():
    bind = op.get_bind()
    inspector = _inspector()

    # Create custom roles once; tolerate a previous MySQL attempt that got this far.
    if not inspector.has_table("role"):
        op.create_table(
            "role",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(length=24), nullable=False),
            sa.Column("label", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["organization_id"], ["organization.id"],
                name="fk_role_organization_id_organization", ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_role"),
            sa.UniqueConstraint("organization_id", "key", name="uq_role_organization_key"),
        )
    if not _has_index("role", "ix_role_organization_id"):
        op.create_index("ix_role_organization_id", "role", ["organization_id"])

    if not _has_column("team_profile", "user_id"):
        op.add_column("team_profile", sa.Column("user_id", sa.Integer(), nullable=True))
    if not _has_fk("team_profile", "fk_team_profile_user_id_user"):
        op.create_foreign_key(
            "fk_team_profile_user_id_user", "team_profile", "user",
            ["user_id"], ["id"], ondelete="SET NULL",
        )
    if not _has_unique("team_profile", "uq_team_profile_user_id"):
        op.create_unique_constraint("uq_team_profile_user_id", "team_profile", ["user_id"])
    if _has_column("team_profile", "last_login_at"):
        op.drop_column("team_profile", "last_login_at")

    if not _has_column("invitation", "profile_id"):
        op.add_column("invitation", sa.Column("profile_id", sa.Integer(), nullable=True))
    if not _has_fk("invitation", "fk_invitation_profile_id_team_profile"):
        op.create_foreign_key(
            "fk_invitation_profile_id_team_profile", "invitation", "team_profile",
            ["profile_id"], ["id"], ondelete="SET NULL",
        )

    # Preserve IDs for existing members. The unique email constraint on
    # (organization_id, email) prevents multiple profiles mapping to one account.
    bind.execute(sa.text(
        "UPDATE team_profile SET user_id = ("
        "SELECT u.id FROM user u "
        "WHERE lower(u.email) = lower(team_profile.email) "
        "AND u.organization_id = team_profile.organization_id LIMIT 1) "
        "WHERE user_id IS NULL"
    ))
    bind.execute(sa.text(
        "UPDATE team_profile SET status = 'active', "
        "joined_at = COALESCE(joined_at, created_at) "
        "WHERE user_id IS NOT NULL AND status = 'invited'"
    ))

    # Dropping the table also drops its FK and supporting index in MySQL.
    # Explicitly dropping ix_team_profile_token_profile_id first fails with 1553.
    if _inspector().has_table("team_profile_token"):
        op.drop_table("team_profile_token")


def downgrade():
    inspector = _inspector()
    if not inspector.has_table("team_profile_token"):
        op.create_table(
            "team_profile_token",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("purpose", sa.String(length=12), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["profile_id"], ["team_profile.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_team_profile_token_profile_id", "team_profile_token", ["profile_id"])

    if _has_fk("invitation", "fk_invitation_profile_id_team_profile"):
        op.drop_constraint("fk_invitation_profile_id_team_profile", "invitation", type_="foreignkey")
    if _has_column("invitation", "profile_id"):
        op.drop_column("invitation", "profile_id")

    if _has_unique("team_profile", "uq_team_profile_user_id"):
        op.drop_constraint("uq_team_profile_user_id", "team_profile", type_="unique")
    if _has_fk("team_profile", "fk_team_profile_user_id_user"):
        op.drop_constraint("fk_team_profile_user_id_user", "team_profile", type_="foreignkey")
    if _has_column("team_profile", "user_id"):
        op.drop_column("team_profile", "user_id")
    if not _has_column("team_profile", "last_login_at"):
        op.add_column("team_profile", sa.Column("last_login_at", sa.DateTime(), nullable=True))

    bind = op.get_bind()
    bind.execute(sa.text("UPDATE user SET `role` = 'contributor' WHERE `role` LIKE 'custom-%'"))
    bind.execute(sa.text("UPDATE invitation SET `role` = 'contributor' WHERE `role` LIKE 'custom-%'"))
    bind.execute(sa.text("DELETE FROM role_permission WHERE `role` LIKE 'custom-%'"))
    if _inspector().has_table("role"):
        op.drop_table("role")
