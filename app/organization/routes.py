from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db, limiter
from app.mail import send_email
from app.auth.models import Invitation, RolePermission, User
from app.organization.models import Organization
from app.organization.roles import (
    assignable_roles,
    create_role,
    custom_roles,
    delete_role,
    get_custom_role,
    rename_role,
    role_labels,
)
from app.permissions import (
    DEFAULT_ROLE_PERMISSIONS,
    PROTECTED_PERMISSIONS,
    ROLE_PERMISSIONS,
    requires_permission,
)
from app.security import encrypt_smtp_password, hash_token, new_token, valid_email
from app.team_profiles.models import STATUS_ACTIVE, STATUS_INVITED, STATUS_SUSPENDED, TeamProfile
from app.team_profiles.services import (
    NAME_LIMIT,
    ROLE_LIMIT,
    create_profile,
    is_public,
    one_line,
    photo_url,
    public_profile_url,
    remove_photo,
)
from app.urls import absolute_app_url
from app.utils import utcnow

organization = Blueprint("organization", __name__)


def require_admin():
    if not current_user.is_org_admin:
        flash("Only organisation admins can change these settings.", "error")
        return redirect(url_for("main.dashboard"))
    return None


@organization.route("/settings/organisation", methods=["GET", "POST"])
@login_required
@requires_permission("settings.manage")
def settings():
    org = db.session.get(Organization, current_user.organization_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        website = request.form.get("website", "").strip()
        if not name or len(name) > 160 or len(website) > 255:
            flash("Enter a valid organisation name and website.", "error")
        else:
            org.name = name
            org.website = website or None
            db.session.commit()
            flash("Organisation details saved.", "success")
            return redirect(url_for("organization.settings"))
    return render_template("organization/settings.html", organization=org)


@organization.route("/settings/email", methods=["GET", "POST"])
@login_required
@requires_permission("settings.manage")
def email_settings():
    org = db.session.get(Organization, current_user.organization_id)
    if request.method == "POST":
        host = request.form.get("host", "").strip()
        sender = request.form.get("sender", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        security = request.form.get("security", "starttls")
        try:
            port = int(request.form.get("port", ""))
        except ValueError:
            port = 0
        if not host or len(host) > 255 or not 1 <= port <= 65535:
            flash("Enter a valid SMTP host and port.", "error")
        elif not valid_email(sender) or security not in {"starttls", "ssl"}:
            flash("Enter a valid sender address and connection security.", "error")
        else:
            try:
                encrypted_password = encrypt_smtp_password(password) if password else None
            except RuntimeError as error:
                flash(str(error), "error")
                return render_template("organization/email_settings.html", organization=org)
            org.smtp_host = host
            org.smtp_port = port
            org.smtp_username = username or None
            org.smtp_sender = sender
            org.smtp_security = security
            if password:
                org.smtp_password_encrypted = encrypted_password
            db.session.commit()
            flash("SMTP settings saved. The password is never shown again.", "success")
            return redirect(url_for("organization.email_settings"))
    return render_template("organization/email_settings.html", organization=org)


INVITATION_LIFETIME = timedelta(days=7)


def _send_invitation(invite, profile, token):
    """Email the invitation link and the member's team ID; True when it was sent."""
    organization = current_user.organization
    try:
        link = absolute_app_url("auth.accept_invitation", token=token)
        role_label = role_labels(invite.organization_id)[invite.role]
        return send_email(
            invite.email,
            f"Join {organization.name} on GAMKERS PORTAL · {profile.team_id}",
            f"You have been invited to join {organization.name} as a {role_label}.\n\n"
            f"Your GAMKERS team ID is {profile.team_id}. After you join, add your photo, "
            "title and social links under My profile. Your public team page will be at:\n"
            f"{public_profile_url(profile)}\n\n"
            f"Accept within 7 days:\n{link}",
            organization,
        )
    except Exception:
        current_app.logger.exception("Organisation invitation email delivery failed.")
        return False


def _invitation_profile(invite):
    """The team profile an invitation belongs to. Invitations sent before profiles
    were merged into team management have none, so one is attached on resend."""
    profile = db.session.get(TeamProfile, invite.profile_id) if invite.profile_id else None
    if profile is None:
        profile = db.session.scalar(
            db.select(TeamProfile).where(
                TeamProfile.organization_id == invite.organization_id,
                func.lower(TeamProfile.email) == invite.email.lower(),
            )
        )
    if profile is None:
        profile = create_profile(invite.organization_id, invite.email.lower(), current_user.id)
    if invite.profile_id != profile.id:
        invite.profile_id = profile.id
        db.session.commit()
    return profile


def _invite_member(email, role, name, role_title):
    organization_id = current_user.organization_id
    profile = db.session.scalar(
        db.select(TeamProfile).where(
            TeamProfile.organization_id == organization_id,
            func.lower(TeamProfile.email) == email,
        )
    )
    created = profile is None
    if profile is not None and profile.status == STATUS_SUSPENDED:
        flash("That team ID is suspended. Reactivate it before sending an invitation.", "error")
        return
    if created:
        try:
            profile = create_profile(
                organization_id, email, current_user.id, name or "", role_title or ""
            )
        except ValueError as error:
            flash(str(error), "error")
            return
    else:
        # A profile from before profiles were merged into team management keeps its ID.
        profile.name = profile.name or name or ""
        profile.role_title = profile.role_title or role_title or ""
    token = new_token()
    invite = Invitation(
        organization_id=organization_id,
        email=email,
        role=role,
        profile_id=profile.id,
        token_hash=hash_token(token),
        expires_at=utcnow() + INVITATION_LIFETIME,
    )
    db.session.add(invite)
    db.session.commit()
    if _send_invitation(invite, profile, token):
        flash(f"Invitation sent. Their team ID is {profile.team_id}.", "success")
        return
    db.session.delete(invite)
    if created:
        db.session.delete(profile)
    db.session.commit()
    flash("Invitation email could not be sent. Configure SMTP and try again.", "error")


@organization.route("/settings/team", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per hour", methods=["POST"])
def team():
    denied = require_admin()
    if denied:
        return denied
    organization_id = current_user.organization_id
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "contributor")
        name = one_line(request.form.get("name"), NAME_LIMIT)
        role_title = one_line(request.form.get("role_title"), ROLE_LIMIT)
        if role not in assignable_roles(organization_id):
            flash("Choose one of the available roles.", "error")
        elif not valid_email(email):
            flash("Enter a valid email address.", "error")
        elif name is None or role_title is None:
            flash("Keep the name and public title under 120 characters.", "error")
        elif db.session.scalar(db.select(User.id).where(func.lower(User.email) == email)):
            flash("That email already has an account.", "error")
        elif db.session.scalar(
            db.select(Invitation.id).where(
                Invitation.organization_id == organization_id,
                Invitation.email == email,
                Invitation.accepted_at.is_(None),
            )
        ):
            flash("An invitation for that email already exists. Resend or revoke it below.", "error")
        else:
            _invite_member(email, role, name, role_title)
        return redirect(url_for("organization.team"))

    labels = role_labels(organization_id)
    members = User.query.filter_by(organization_id=organization_id).order_by(User.created_at).all()
    profiles = TeamProfile.query.filter_by(organization_id=organization_id).order_by(TeamProfile.team_id).all()
    invitations = (
        Invitation.query.filter_by(organization_id=organization_id, accepted_at=None)
        .order_by(Invitation.created_at.desc())
        .all()
    )
    now = utcnow()
    pending_profile_ids = {invite.profile_id for invite in invitations if invite.profile_id}
    return render_template(
        "organization/team.html",
        members=members,
        profile_by_user={p.user_id: p for p in profiles if p.user_id},
        profile_by_id={p.id: p for p in profiles},
        loose_profiles=[
            p for p in profiles if p.user_id is None and p.id not in pending_profile_ids
        ],
        invitations=invitations,
        expired_invitation_ids={invite.id for invite in invitations if invite.expires_at <= now},
        roles=labels,
        assignable=assignable_roles(organization_id),
        custom_roles=custom_roles(organization_id),
        permission_rows=ROLE_PERMISSIONS,
        permission_defaults=DEFAULT_ROLE_PERMISSIONS,
        permission_overrides={
            (item.role, item.permission): item.enabled
            for item in RolePermission.query.filter_by(organization_id=organization_id).all()
        },
        protected_permissions=PROTECTED_PERMISSIONS,
        is_public=is_public,
        photo_url=photo_url,
    )


# ── Invitations ─────────────────────────────────────────────────────────

def _pending_invitation(invitation_id):
    return Invitation.query.filter_by(
        id=invitation_id, organization_id=current_user.organization_id, accepted_at=None
    ).first_or_404()


@organization.post("/settings/team/invitations/<int:invitation_id>/resend")
@login_required
@limiter.limit("20 per hour")
def resend_invitation(invitation_id):
    denied = require_admin()
    if denied:
        return denied
    invite = _pending_invitation(invitation_id)
    if invite.role not in assignable_roles(invite.organization_id):
        flash("This invitation's role no longer exists. Revoke it and invite them again.", "error")
        return redirect(url_for("organization.team"))
    try:
        profile = _invitation_profile(invite)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("organization.team"))
    if profile.status == STATUS_SUSPENDED:
        flash("Reactivate this team ID before sending an invitation.", "error")
        return redirect(url_for("organization.team"))
    token = new_token()
    if _send_invitation(invite, profile, token):
        invite.token_hash = hash_token(token)
        invite.expires_at = utcnow() + INVITATION_LIFETIME
        db.session.commit()
        flash(f"Invitation re-sent to {invite.email}.", "success")
    else:
        flash("The invitation email could not be sent. Check the email settings.", "error")
    return redirect(url_for("organization.team"))


@organization.post("/settings/team/invitations/<int:invitation_id>/revoke")
@login_required
def revoke_invitation(invitation_id):
    denied = require_admin()
    if denied:
        return denied
    invite = _pending_invitation(invitation_id)
    profile = db.session.get(TeamProfile, invite.profile_id) if invite.profile_id else None
    email, photo = invite.email, None
    db.session.delete(invite)
    # Drop a profile that never belonged to anyone, so its team ID doesn't linger.
    if (
        profile is not None
        and profile.user_id is None
        and profile.status == STATUS_INVITED
        and profile.joined_at is None
    ):
        photo = profile.photo_path
        db.session.delete(profile)
    db.session.commit()
    remove_photo(photo)
    flash(f"Invitation for {email} revoked.", "success")
    return redirect(url_for("organization.team"))


# ── Members and their team profiles ─────────────────────────────────────

@organization.post("/settings/team/<int:user_id>/role")
@login_required
def update_role(user_id):
    denied = require_admin()
    if denied:
        return denied
    user = User.query.filter_by(
        id=user_id, organization_id=current_user.organization_id
    ).first_or_404()
    role = request.form.get("role")
    if user.id == current_user.id:
        flash("You cannot change your own role.", "error")
    elif role not in assignable_roles(current_user.organization_id):
        flash(
            "Choose the developer or contributor role, or one of your custom roles. "
            "The single admin cannot be reassigned.",
            "error",
        )
    else:
        user.role = role
        user.admin_slot = None
        db.session.commit()
        flash("Member role updated.", "success")
    return redirect(url_for("organization.team"))


def _team_profile(profile_id):
    return TeamProfile.query.filter_by(
        id=profile_id, organization_id=current_user.organization_id
    ).first_or_404()


@organization.post("/settings/team/profiles/<int:profile_id>/update")
@login_required
def update_profile(profile_id):
    denied = require_admin()
    if denied:
        return denied
    profile = _team_profile(profile_id)
    name = one_line(request.form.get("name"), NAME_LIMIT)
    role_title = one_line(request.form.get("role_title"), ROLE_LIMIT)
    if name is None or role_title is None:
        flash("Keep the name and public title under 120 characters.", "error")
    else:
        profile.name, profile.role_title = name, role_title
        profile.is_leader = request.form.get("is_leader") == "on"
        db.session.commit()
        flash(f"{profile.team_id} updated.", "success")
    return redirect(url_for("organization.team"))


@organization.post("/settings/team/profiles/<int:profile_id>/status")
@login_required
def update_profile_status(profile_id):
    denied = require_admin()
    if denied:
        return denied
    profile = _team_profile(profile_id)
    action = request.form.get("action")
    if action == "suspend":
        profile.status = STATUS_SUSPENDED
        flash(f"{profile.team_id} suspended. Its public page now shows it as inactive.", "success")
    elif action == "reactivate":
        profile.status = STATUS_ACTIVE if profile.joined_at else STATUS_INVITED
        flash(f"{profile.team_id} reactivated.", "success")
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("organization.team"))


@organization.post("/settings/team/profiles/<int:profile_id>/delete")
@login_required
def delete_profile(profile_id):
    denied = require_admin()
    if denied:
        return denied
    profile = _team_profile(profile_id)
    if profile.user_id is not None:
        flash("This profile belongs to a portal member. Suspend it instead.", "error")
        return redirect(url_for("organization.team"))
    team_id, photo = profile.team_id, profile.photo_path
    Invitation.query.filter_by(profile_id=profile.id, accepted_at=None).delete()
    db.session.delete(profile)
    db.session.commit()
    remove_photo(photo)
    flash(f"{team_id} deleted. Its public link no longer works.", "success")
    return redirect(url_for("organization.team"))


# ── Roles and permissions ───────────────────────────────────────────────

@organization.post("/settings/team/roles")
@login_required
def add_role():
    denied = require_admin()
    if denied:
        return denied
    try:
        role = create_role(
            current_user.organization_id,
            request.form.get("name"),
            request.form.get("copy_from"),
        )
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash(f"Role {role.label} created. Set what it can do under Role permissions.", "success")
    return redirect(url_for("organization.team"))


def _custom_role_or_404(key):
    role = get_custom_role(current_user.organization_id, key)
    if role is None:
        abort(404)
    return role


@organization.post("/settings/team/roles/<key>/update")
@login_required
def edit_role(key):
    denied = require_admin()
    if denied:
        return denied
    role = _custom_role_or_404(key)
    try:
        rename_role(role, request.form.get("name"))
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Role renamed.", "success")
    return redirect(url_for("organization.team"))


@organization.post("/settings/team/roles/<key>/delete")
@login_required
def remove_role(key):
    denied = require_admin()
    if denied:
        return denied
    role = _custom_role_or_404(key)
    label = role.label
    try:
        delete_role(role)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash(f"Role {label} deleted.", "success")
    return redirect(url_for("organization.team"))


@organization.post("/settings/team/permissions")
@login_required
def update_permissions():
    denied = require_admin()
    if denied:
        return denied
    role_permissions = {
        (item.role, item.permission): item
        for item in RolePermission.query.filter_by(
            organization_id=current_user.organization_id
        ).all()
    }
    for role in role_labels(current_user.organization_id):
        for permission, _label in ROLE_PERMISSIONS:
            enabled = request.form.get(f"{role}:{permission}") == "on"
            if permission in PROTECTED_PERMISSIONS:
                enabled = role == "admin"
            existing = role_permissions.get((role, permission))
            if existing is None:
                existing = RolePermission(
                    organization_id=current_user.organization_id,
                    role=role,
                    permission=permission,
                    enabled=enabled,
                )
                db.session.add(existing)
            else:
                existing.enabled = enabled
    db.session.commit()
    flash("Role permissions updated.", "success")
    return redirect(url_for("organization.team"))
