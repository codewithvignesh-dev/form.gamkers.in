"""Roles: the built-in ones plus the custom roles an admin creates."""

import secrets

from sqlalchemy import func

from app.auth.models import Invitation, Role, RolePermission, User
from app.extensions import db
from app.permissions import (
    CUSTOM_ROLE_PREFIX,
    DEFAULT_ROLE_PERMISSIONS,
    MAX_CUSTOM_ROLES,
    PROTECTED_PERMISSIONS,
    ROLE_LABEL_LIMIT,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
)
from app.utils import utcnow


def custom_roles(organization_id):
    return db.session.scalars(
        db.select(Role).where(Role.organization_id == organization_id).order_by(Role.label)
    ).all()


def get_custom_role(organization_id, key):
    return db.session.scalar(
        db.select(Role).where(Role.organization_id == organization_id, Role.key == key)
    )


def role_labels(organization_id):
    """Every role key and label for the organisation: built-in roles, then custom ones."""
    labels = dict(ROLE_LABELS)
    for role in custom_roles(organization_id):
        labels[role.key] = role.label
    return labels


def assignable_roles(organization_id):
    """Roles that can be given to a member. The single admin account is never assignable."""
    labels = role_labels(organization_id)
    labels.pop("admin")
    return labels


def clean_label(value):
    label = " ".join((value or "").split())
    return label if label and len(label) <= ROLE_LABEL_LIMIT else None


def _label_in_use(organization_id, label, ignore_key=None):
    wanted = label.casefold()
    return any(
        existing.casefold() == wanted
        for key, existing in role_labels(organization_id).items()
        if key != ignore_key
    )


def effective_permissions(organization_id, role):
    """The permissions a role holds now: its defaults changed by any saved overrides."""
    granted = set(DEFAULT_ROLE_PERMISSIONS.get(role, ()))
    rows = db.session.scalars(
        db.select(RolePermission).where(
            RolePermission.organization_id == organization_id, RolePermission.role == role
        )
    ).all()
    for row in rows:
        (granted.add if row.enabled else granted.discard)(row.permission)
    return granted


def create_role(organization_id, label, copy_from=None):
    """Create a custom role, optionally starting from another role's permissions."""
    label = clean_label(label)
    if label is None:
        raise ValueError(f"Enter a role name of up to {ROLE_LABEL_LIMIT} characters.")
    if len(custom_roles(organization_id)) >= MAX_CUSTOM_ROLES:
        raise ValueError(f"You can create up to {MAX_CUSTOM_ROLES} custom roles.")
    if _label_in_use(organization_id, label):
        raise ValueError("A role with that name already exists.")
    granted = set()
    if copy_from in role_labels(organization_id):
        granted = effective_permissions(organization_id, copy_from) - PROTECTED_PERMISSIONS
    role = Role(
        organization_id=organization_id,
        key=CUSTOM_ROLE_PREFIX + secrets.token_hex(4),
        label=label,
    )
    db.session.add(role)
    for permission, _description in ROLE_PERMISSIONS:
        db.session.add(
            RolePermission(
                organization_id=organization_id,
                role=role.key,
                permission=permission,
                enabled=permission in granted,
            )
        )
    db.session.commit()
    return role


def rename_role(role, label):
    label = clean_label(label)
    if label is None:
        raise ValueError(f"Enter a role name of up to {ROLE_LABEL_LIMIT} characters.")
    if _label_in_use(role.organization_id, label, ignore_key=role.key):
        raise ValueError("A role with that name already exists.")
    role.label = label
    db.session.commit()


def delete_role(role):
    """Delete a custom role that nobody holds and no open invitation offers."""
    holders = db.session.scalar(
        db.select(func.count(User.id)).where(
            User.organization_id == role.organization_id, User.role == role.key
        )
    )
    if holders:
        raise ValueError(
            f"{holders} member{'s have' if holders != 1 else ' has'} this role. "
            "Move them to another role first."
        )
    open_invitations = db.session.scalar(
        db.select(func.count(Invitation.id)).where(
            Invitation.organization_id == role.organization_id,
            Invitation.role == role.key,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > utcnow(),
        )
    )
    if open_invitations:
        raise ValueError("A pending invitation uses this role. Revoke it first.")
    db.session.execute(
        db.delete(RolePermission).where(
            RolePermission.organization_id == role.organization_id,
            RolePermission.role == role.key,
        )
    )
    db.session.delete(role)
    db.session.commit()
