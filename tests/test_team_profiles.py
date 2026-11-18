import io
import re

import pytest
from PIL import Image

from app.auth.models import Invitation, Role, RolePermission, User
from app.extensions import db
from app.team_profiles.models import TeamProfile

ADMIN_EMAIL = "team-admin@example.com"
ADMIN_PASSWORD = "safe-test-password-2026"
MEMBER_PASSWORD = "member-test-password-2026"


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    def fake_send(recipient, subject, body, organization=None, **kwargs):
        sent.append({"to": recipient, "subject": subject, "body": body})
        return True

    monkeypatch.setattr("app.organization.routes.send_email", fake_send)
    return sent


def _sign_in_admin(client):
    result = client.application.test_cli_runner().invoke(
        args=["create-admin", "--name", "Team Admin", "--email", ADMIN_EMAIL],
        input=f"{ADMIN_PASSWORD}\n{ADMIN_PASSWORD}\n",
    )
    assert result.exit_code == 0, result.output
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})


def _invite_path(mail):
    match = re.search(r"https://forms\.example(/invite/[\w-]+)", mail["body"])
    assert match, mail["body"]
    return match.group(1)


def _png(size=(900, 600), color=(30, 160, 80)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _invite_and_join(client, outbox, email="dev@example.com", role="developer", **extra):
    """Invite from /settings/team, then accept as the new member (signed in afterwards)."""
    client.post("/settings/team", data={"email": email, "role": role, **extra})
    link = _invite_path(outbox[-1])
    client.post("/logout")
    accepted = client.post(link, data={"name": "Dev Kumar", "password": MEMBER_PASSWORD})
    assert accepted.status_code == 302
    return TeamProfile.query.filter_by(email=email).one()


def _sign_back_in_as_admin(client):
    client.post("/logout")
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})


def test_invitation_creates_the_team_profile_with_sequential_ids(client, outbox):
    _sign_in_admin(client)
    response = client.post(
        "/settings/team",
        data={"email": "Priya@Example.com", "role": "developer",
              "name": "Priya Raman", "role_title": "VAPT Lead"},
        follow_redirects=True,
    )
    assert b"GMK-0001" in response.data
    client.post("/settings/team", data={"email": "arun@example.com", "role": "contributor"})
    assert [p.team_id for p in TeamProfile.query.order_by(TeamProfile.id)] == ["GMK-0001", "GMK-0002"]

    first = TeamProfile.query.filter_by(team_id="GMK-0001").one()
    assert first.email == "priya@example.com" and first.status == "invited" and first.user_id is None
    assert first.name == "Priya Raman" and first.role_title == "VAPT Lead"
    assert outbox[0]["to"] == "priya@example.com"
    assert "GMK-0001" in outbox[0]["subject"]
    assert "https://forms.example/team/GMK-0001" in outbox[0]["body"]
    assert Invitation.query.filter_by(email="priya@example.com").one().profile_id == first.id

    duplicate = client.post(
        "/settings/team", data={"email": "PRIYA@example.com", "role": "developer"},
        follow_redirects=True,
    )
    assert b"already exists" in duplicate.data
    assert TeamProfile.query.count() == 2 and len(outbox) == 2


def test_failed_invitation_email_leaves_no_profile_behind(client, monkeypatch):
    _sign_in_admin(client)
    monkeypatch.setattr("app.organization.routes.send_email", lambda *a, **k: False)
    response = client.post(
        "/settings/team", data={"email": "x@example.com", "role": "developer"}, follow_redirects=True
    )
    assert b"could not be sent" in response.data
    assert TeamProfile.query.count() == 0 and Invitation.query.count() == 0


def test_accepting_the_invitation_links_the_account_to_the_profile(client, outbox):
    _sign_in_admin(client)
    profile = _invite_and_join(client, outbox)
    user = User.query.filter_by(email="dev@example.com").one()
    db.session.refresh(profile)
    assert profile.user_id == user.id and profile.status == "active" and profile.joined_at
    assert profile.name == "Dev Kumar"
    # the invitation link works exactly once
    assert client.post("/logout").status_code in (200, 302)
    reused = client.post(
        "/invite/whatever", data={"name": "Eve", "password": MEMBER_PASSWORD}, follow_redirects=True
    )
    assert b"invalid or has expired" in reused.data


def test_member_edits_their_profile_and_publishes_the_verified_page(client, outbox):
    _sign_in_admin(client)
    profile = _invite_and_join(client, outbox)
    assert client.get("/team/GMK-0001").status_code == 404  # hidden until title is set

    assert client.get("/settings/profile").status_code == 200
    bad = client.post("/settings/profile", data={
        "name": "Dev Kumar", "role_title": "Hardware Lead",
        "linkedin": "https://linkedin.com.evil.example/in/dev",
        "website": "javascript:alert(1)",
    })
    assert bad.status_code == 400
    assert b"LinkedIn links must point to linkedin.com" in bad.data
    assert b"Enter a valid Website link." in bad.data
    assert b'value="Dev Kumar"' in bad.data

    saved = client.post("/settings/profile", data={
        "name": "Dev Kumar", "role_title": "Hardware Lead",
        "bio": "Builds <b>badges</b> & RF tools.",
        "linkedin": "linkedin.com/in/devkumar",
        "github": "https://github.com/devk",
        "photo": (_png(), "me.png"),
    }, content_type="multipart/form-data")
    assert saved.status_code == 302
    db.session.refresh(profile)
    assert profile.socials == {
        "linkedin": "https://linkedin.com/in/devkumar",
        "github": "https://github.com/devk",
    }

    client.post("/logout")
    page = client.get("/team/GMK-0001")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Verified GAMKERS team member" in html and "Hardware Lead" in html
    assert "https://github.com/devk" in html
    assert "&lt;b&gt;badges&lt;/b&gt;" in html
    assert "dev@example.com" not in html
    assert 'property="og:image"' in html

    photo = client.get(f"/team/GMK-0001/photo?v={profile.photo_path[:8]}")
    assert photo.status_code == 200 and photo.mimetype == "image/jpeg"
    with Image.open(io.BytesIO(photo.data)) as image:
        assert image.size == (640, 640) and not image.getexif()
    assert client.get("/team/gmk-0001").headers["Location"].endswith("/team/GMK-0001")
    assert client.get("/team?team_id=gmk-0001").status_code == 302


def test_admin_gets_a_profile_the_first_time_they_open_it(client):
    _sign_in_admin(client)
    assert client.get("/settings/profile").status_code == 200
    profile = TeamProfile.query.one()
    assert profile.email == ADMIN_EMAIL and profile.user_id and profile.status == "active"


def test_signed_out_visitors_cannot_open_the_profile_editor(client):
    assert client.get("/settings/profile").status_code == 302


def test_suspend_reactivate_and_delete_from_team_settings(client, outbox):
    _sign_in_admin(client)
    profile = _invite_and_join(client, outbox)
    client.post("/settings/profile", data={"name": "Dev Kumar", "role_title": "Hardware Lead"})
    assert client.get("/team/GMK-0001").status_code == 200

    _sign_back_in_as_admin(client)
    client.post(f"/settings/team/profiles/{profile.id}/status", data={"action": "suspend"})
    inactive = client.get("/team/GMK-0001")
    assert inactive.status_code == 410
    assert b"no longer active" in inactive.data and b"Dev Kumar" not in inactive.data

    client.post(f"/settings/team/profiles/{profile.id}/status", data={"action": "reactivate"})
    assert client.get("/team/GMK-0001").status_code == 200

    # a profile that belongs to a portal member can only be suspended, never deleted
    client.post(f"/settings/team/profiles/{profile.id}/delete")
    assert client.get("/team/GMK-0001").status_code == 200

    client.post(f"/settings/team/profiles/{profile.id}/update",
                data={"name": "Dev K", "role_title": "Lead"})
    db.session.refresh(profile)
    assert (profile.name, profile.role_title) == ("Dev K", "Lead")


def test_resend_and_revoke_pending_invitations(client, outbox):
    _sign_in_admin(client)
    client.post("/settings/team", data={"email": "later@example.com", "role": "contributor"})
    invite = Invitation.query.filter_by(email="later@example.com").one()
    old_link = _invite_path(outbox[0])

    client.post(f"/settings/team/invitations/{invite.id}/resend")
    assert len(outbox) == 2 and _invite_path(outbox[1]) != old_link
    assert "GMK-0001" in outbox[1]["subject"]

    client.post(f"/settings/team/invitations/{invite.id}/revoke")
    assert Invitation.query.count() == 0 and TeamProfile.query.count() == 0
    client.post("/logout")
    assert b"invalid or has expired" in client.post(
        _invite_path(outbox[1]), data={"name": "Late", "password": MEMBER_PASSWORD},
        follow_redirects=True,
    ).data


def test_deleted_team_ids_are_never_reissued(client, outbox):
    _sign_in_admin(client)
    client.post("/settings/team", data={"email": "first@example.com", "role": "developer"})
    client.post("/settings/team", data={"email": "second@example.com", "role": "developer"})
    newest = TeamProfile.query.filter_by(team_id="GMK-0002").one()
    client.post(f"/settings/team/profiles/{newest.id}/delete")

    client.post("/settings/team", data={"email": "third@example.com", "role": "developer"})
    assert TeamProfile.query.filter_by(email="third@example.com").one().team_id == "GMK-0003"
    assert client.get("/team/GMK-0002").status_code == 404


def test_existing_unlinked_profile_is_reused_when_the_email_is_invited(client, outbox):
    _sign_in_admin(client)
    legacy = TeamProfile(
        organization_id=User.query.one().organization_id, team_id="GMK-0042",
        email="old@example.com", name="Old Hand", role_title="Founder", status="active",
    )
    db.session.add(legacy)
    db.session.commit()
    client.post("/settings/team", data={"email": "old@example.com", "role": "contributor"})
    assert TeamProfile.query.count() == 1
    assert "GMK-0042" in outbox[0]["subject"]
    client.post("/logout")
    client.post(_invite_path(outbox[0]), data={"name": "Old Hand", "password": MEMBER_PASSWORD})
    db.session.refresh(legacy)
    assert legacy.user_id == User.query.filter_by(email="old@example.com").one().id


def test_non_admins_cannot_manage_the_team(client, outbox):
    _sign_in_admin(client)
    profile = _invite_and_join(client, outbox)
    for path in (
        "/settings/team",
        f"/settings/team/profiles/{profile.id}/status",
        "/settings/team/roles",
        "/settings/team/permissions",
    ):
        assert client.post(path, data={"action": "suspend", "name": "x"}).status_code in (302, 403), path
    db.session.refresh(profile)
    assert profile.status == "active" and Role.query.count() == 0


def test_admin_creates_renames_and_deletes_custom_roles(client, outbox):
    _sign_in_admin(client)
    created = client.post(
        "/settings/team/roles", data={"name": "Reviewer", "copy_from": "developer"}, follow_redirects=True
    )
    assert b"Reviewer" in created.data
    role = Role.query.one()
    assert role.key.startswith("custom-") and role.label == "Reviewer"
    assert RolePermission.query.filter_by(role=role.key, enabled=True).count() > 0
    assert not RolePermission.query.filter_by(role=role.key, permission="team.manage", enabled=True).count()

    duplicate = client.post("/settings/team/roles", data={"name": "reviewer"}, follow_redirects=True)
    assert b"already" in duplicate.data and Role.query.count() == 1
    clash = client.post("/settings/team/roles", data={"name": "Admin"}, follow_redirects=True)
    assert Role.query.count() == 1 and b"already" in clash.data

    client.post(f"/settings/team/roles/{role.key}/update", data={"name": "QA Reviewer"})
    db.session.refresh(role)
    assert role.label == "QA Reviewer"

    # invite someone with the custom role: it can't be deleted while held or pending
    client.post("/settings/team", data={"email": "qa@example.com", "role": role.key})
    blocked = client.post(f"/settings/team/roles/{role.key}/delete", follow_redirects=True)
    assert Role.query.count() == 1 and b"QA Reviewer" in blocked.data
    client.post("/logout")
    client.post(_invite_path(outbox[-1]), data={"name": "Quinn", "password": MEMBER_PASSWORD})
    assert User.query.filter_by(email="qa@example.com").one().role == role.key

    _sign_back_in_as_admin(client)
    client.post(f"/settings/team/roles/{role.key}/delete")
    assert Role.query.count() == 1  # still held by Quinn
    quinn = User.query.filter_by(email="qa@example.com").one()
    client.post(f"/settings/team/{quinn.id}/role", data={"role": "contributor"})
    client.post(f"/settings/team/roles/{role.key}/delete")
    assert Role.query.count() == 0
    assert RolePermission.query.filter_by(role=role.key).count() == 0


def test_custom_role_permissions_are_editable_and_enforced(client, outbox):
    _sign_in_admin(client)
    client.post("/settings/team/roles", data={"name": "Viewer"})
    role = Role.query.one()
    client.post("/settings/team", data={"email": "view@example.com", "role": role.key})
    client.post("/logout")
    client.post(_invite_path(outbox[-1]), data={"name": "Vee", "password": MEMBER_PASSWORD})
    member = User.query.filter_by(email="view@example.com").one()
    assert not member.has_permission("team.manage")

    _sign_back_in_as_admin(client)
    page = client.get("/settings/team").get_data(as_text=True)
    assert f'name="{role.key}:forms.manage"' in page

    # protected permissions can't be given to a custom role, even by a crafted post
    client.post("/settings/team/permissions", data={
        f"{role.key}:team.manage": "on", f"{role.key}:permissions.manage": "on",
    })
    db.session.expire_all()
    assert not member.has_permission("team.manage")
    assert not member.has_permission("permissions.manage")
    assert client.get("/settings/team").status_code == 200


def test_old_team_portal_routes_are_gone(client):
    for path in ("/portal", "/portal/login", "/team-profiles"):
        assert client.get(path).status_code == 404, path


def test_old_id_links_redirect_to_team_pages(client):
    assert client.get("/id/GMK-0007").headers["Location"].endswith("/team/GMK-0007")
    assert client.get("/id/GMK-0007").status_code == 301
    assert client.get("/id/GMK-0007/photo?v=ab").headers["Location"].endswith("/team/GMK-0007/photo?v=ab")
    assert client.get("/id?team_id=GMK-0007").headers["Location"].endswith("/team?team_id=GMK-0007")


def test_admin_marks_a_member_as_a_landing_page_leader(client, outbox):
    _sign_in_admin(client)
    profile = _invite_and_join(client, outbox)
    client.post("/settings/profile", data={"name": "Dev Kumar", "role_title": "VAPT Lead"})
    assert b'class="l-card l-leader" href="/team/GMK-0001"' not in client.get("/").data

    _sign_back_in_as_admin(client)
    client.post(f"/settings/team/profiles/{profile.id}/update",
                data={"name": "Dev Kumar", "role_title": "VAPT Lead", "is_leader": "on"})
    db.session.refresh(profile)
    assert profile.is_leader is True
    page = client.get("/").get_data(as_text=True)
    assert 'class="l-card l-leader" href="/team/GMK-0001"' in page and "VAPT Lead" in page

    client.post(f"/settings/team/profiles/{profile.id}/update",
                data={"name": "Dev Kumar", "role_title": "VAPT Lead"})
    db.session.refresh(profile)
    assert profile.is_leader is False
