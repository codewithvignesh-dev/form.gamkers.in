"""The public site must keep working, and never leak errors, when MySQL is down.

The "down" app points at a real MySQL URL nobody is listening on, so these tests
exercise the genuine connection-refused path through SQLAlchemy and PyMySQL.
"""

import types

import pytest
from sqlalchemy.exc import OperationalError

from app import create_app
from app.auth.models import User
from app.extensions import db
from app.organization.models import Organization
from app.team_profiles.landing import FALLBACK_LEADERS, initials, role_icon
from app.team_profiles.models import TeamProfile

UNREACHABLE_MYSQL = "mysql+pymysql://portal:secret@127.0.0.1:1/portal"


def _config(tmp_path, **extra):
    return {
        "TESTING": True,
        "SECRET_KEY": "test-key-not-for-production",
        "APPLICATION_BASE_URL": "https://forms.example",
        "CERTIFICATE_STORAGE_PATH": str(tmp_path / "certificates"),
        "CERTIFICATE_UPLOAD_PATH": str(tmp_path / "certificate-uploads"),
        "FORM_BANNER_STORAGE_PATH": str(tmp_path / "form-banners"),
        "TEAM_PHOTO_STORAGE_PATH": str(tmp_path / "team-photos"),
        "SESSION_COOKIE_SECURE": False,
        "REMEMBER_COOKIE_SECURE": False,
        "WTF_CSRF_ENABLED": False,
        "RATELIMIT_ENABLED": False,
        "SMTP_ENCRYPTION_KEY": None,
        "DEFAULT_SMTP_HOST": None,
        "DEFAULT_SMTP_SENDER": None,
        "HOSTINGER_MAIL_API_TOKEN": None,
        **extra,
    }


@pytest.fixture
def down_client(tmp_path):
    app = create_app(_config(tmp_path, SQLALCHEMY_DATABASE_URI=UNREACHABLE_MYSQL))
    with app.app_context():
        yield app.test_client()


def _signed_in_cookie(client):
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True


def _assert_clean(response):
    body = response.get_data(as_text=True)
    for leak in ("Traceback", "OperationalError", "pymysql", "sqlalchemy", "127.0.0.1"):
        assert leak not in body, leak


def test_app_starts_and_landing_page_renders_with_mysql_down(down_client):
    response = down_client.get("/")
    assert response.status_code == 200
    _assert_clean(response)
    html = response.get_data(as_text=True)
    # events are empty, but the team section uses the built-in list
    for leader in FALLBACK_LEADERS:
        assert leader["name"] in html
    # contributors are never static: with no data the whole strip is left out
    assert "Our contributors" not in html and 'class="l-names' not in html
    assert "Join Community" in html


def test_landing_page_ignores_a_stale_session_while_mysql_is_down(down_client):
    _signed_in_cookie(down_client)
    response = down_client.get("/")
    assert response.status_code == 200 and b"Join Community" in response.data
    assert b"c-nav-admin" not in response.data  # rendered as signed out, no error


@pytest.mark.parametrize("path", ["/products/ghostchip", "/products/phantom32", "/login",
                                   "/forgot-password", "/team", "/id", "/certificates/verify"])
def test_pages_that_need_no_data_stay_up_with_mysql_down(down_client, path):
    response = down_client.get(path)
    assert response.status_code in (200, 301, 302), (path, response.status_code)
    _assert_clean(response)


@pytest.mark.parametrize("path", [
    "/f/abc123", "/f/abc123/banner", "/team/GMK-0001", "/team/GMK-0001/photo",
    "/invite/token", "/reset-password/token", "/certificates/public/token",
    "/certificates/verify?token=abc",
])
def test_data_pages_answer_503_not_500_with_mysql_down(down_client, path):
    response = down_client.get(path)
    assert response.status_code == 503, (path, response.status_code)
    assert response.headers["Retry-After"] == "30"
    assert "no-store" in response.headers["Cache-Control"]
    assert b"We'll be right back" in response.data
    _assert_clean(response)


@pytest.mark.parametrize("path,data", [
    ("/login", {"email": "a@example.com", "password": "x" * 12}),
    ("/forgot-password", {"email": "a@example.com"}),
    ("/f/abc123", {"email": "a@example.com"}),
])
def test_form_posts_answer_503_with_mysql_down(down_client, path, data):
    response = down_client.post(path, data=data)
    assert response.status_code == 503, (path, response.status_code)
    assert b"Nothing you submitted was saved" in response.data
    _assert_clean(response)


def test_signed_in_member_gets_503_not_a_login_redirect_while_mysql_is_down(down_client):
    _signed_in_cookie(down_client)
    response = down_client.get("/dashboard")
    assert response.status_code == 503
    assert b"We'll be right back" in response.data


def test_signed_out_visitor_is_still_sent_to_sign_in(down_client):
    response = down_client.get("/dashboard")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_logout_works_while_mysql_is_down(down_client):
    _signed_in_cookie(down_client)
    response = down_client.post("/logout")
    assert response.status_code == 302 and response.headers["Location"].endswith("/")


def test_an_outage_does_not_destroy_the_members_session(down_client):
    _signed_in_cookie(down_client)
    assert down_client.get("/").status_code == 200
    with down_client.session_transaction() as session:
        assert session["_user_id"] == "1"  # still signed in once the database returns


class _BrokenQuery:
    def __getattr__(self, _name):
        raise OperationalError("SELECT 1", {}, Exception("server has gone away"))


def test_landing_page_degrades_when_the_database_fails_midway(client, monkeypatch):
    monkeypatch.setattr("app.main.routes.Form", types.SimpleNamespace(query=_BrokenQuery()))
    response = client.get("/")
    assert response.status_code == 200
    assert FALLBACK_LEADERS[0]["name"] in response.get_data(as_text=True)


def test_unexpected_errors_show_a_plain_500_page_without_details(app):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.add_url_rule("/boom", "boom", lambda: 1 / 0)
    response = app.test_client().get("/boom")
    assert response.status_code == 500
    assert b"That didn" in response.data and b"ZeroDivisionError" not in response.data
    assert "no-store" in response.headers["Cache-Control"]


def test_database_error_during_a_normal_request_becomes_503(app, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("Lost connection to MySQL server"))

    monkeypatch.setattr("app.forms.routes.db.session.scalar", fail)
    response = app.test_client().get("/f/anything")
    assert response.status_code == 503 and b"We'll be right back" in response.data


# ── Landing team section ────────────────────────────────────────────────

def _profile(org_id, number, name, title, *, leader=False, status="active", role="contributor"):
    """A team profile linked to a portal account holding `role` (None: no account)."""
    profile = TeamProfile(
        organization_id=org_id, team_id=f"GMK-{number:04d}", email=f"p{number}@example.com",
        name=name, role_title=title, status=status, is_leader=leader,
    )
    if role:
        user = User(organization_id=org_id, name=name, email=profile.email, role=role)
        user.set_password("a-long-test-password")
        db.session.add(user)
        db.session.flush()
        profile.user_id = user.id
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def org_id(app):
    org = Organization(name="Org")
    db.session.add(org)
    db.session.commit()
    return org.id


def test_landing_without_profiles_uses_builtin_leaders_and_no_contributors(client):
    html = client.get("/").get_data(as_text=True)
    assert all(leader["name"] in html for leader in FALLBACK_LEADERS)
    assert "Our contributors" not in html


def test_contributors_strip_lists_only_members_with_the_contributor_role(client, org_id):
    _profile(org_id, 1, "Priya Raman", "VAPT Lead", leader=True, role="developer")
    _profile(org_id, 2, "Arun Kumar", "Hardware Hacker")
    _profile(org_id, 3, "Dev Person", "Developer", role="developer")
    _profile(org_id, 4, "Boss Person", "Founder", role="admin")
    _profile(org_id, 5, "No Account", "Writer", role=None)
    _profile(org_id, 6, "Hidden Person", "Researcher", status="suspended")
    _profile(org_id, 7, "No Title", "")
    _profile(org_id, 8, "Invited Person", "Writer", status="invited")
    _profile(org_id, 9, "Leader Contributor", "Lead", leader=True)
    html = client.get("/").get_data(as_text=True)
    assert "Priya Raman" in html and 'href="/team/GMK-0001"' in html  # leader card
    strip = html[html.index('class="l-names'):html.index('class="l-collab')]
    assert "Arun Kumar" in strip and 'href="/team/GMK-0002"' in strip
    for other in ("Priya Raman", "Dev Person", "Boss Person", "No Account", "Hidden Person",
                  "No Title", "Invited Person", "Leader Contributor"):
        assert other not in strip, other
    assert 'class="l-count">1<' in html
    # the database now owns the leader cards too
    assert FALLBACK_LEADERS[0]["name"] not in html


def test_changing_a_members_role_moves_them_in_or_out_of_the_strip(client, org_id):
    profile = _profile(org_id, 1, "Arun Kumar", "Hardware Hacker")
    assert "Arun Kumar" in client.get("/").get_data(as_text=True)
    db.session.get(User, profile.user_id).role = "developer"
    db.session.commit()
    assert "Our contributors" not in client.get("/").get_data(as_text=True)


def test_landing_shows_uploaded_photos_and_initials_otherwise(client, org_id):
    with_photo = _profile(org_id, 1, "Priya Raman", "VAPT Lead", leader=True)
    with_photo.photo_path = "abcdef0123456789.jpg"
    _profile(org_id, 2, "Arun Kumar", "Hardware Hacker", leader=True)
    contributor = _profile(org_id, 3, "Meena S", "Researcher")
    contributor.photo_path = "0123456789abcdef.jpg"
    db.session.commit()
    html = client.get("/").get_data(as_text=True)
    assert "/team/GMK-0001/photo?v=abcdef01" in html and 'alt="Photo of Priya Raman"' in html
    assert "/team/GMK-0003/photo?v=01234567" in html
    # no photo: initials stay, and no image is requested for that person
    assert "<span>AK</span>" in html and "GMK-0002/photo" not in html


def test_without_a_leader_the_builtin_leader_cards_stay(client, org_id):
    _profile(org_id, 1, "Arun Kumar", "Hardware Hacker")
    html = client.get("/").get_data(as_text=True)
    assert FALLBACK_LEADERS[0]["name"] in html and "Arun Kumar" in html


def test_team_names_are_escaped_on_the_landing_page(client, org_id):
    _profile(org_id, 1, "<script>alert(1)</script>", "<b>Lead</b>", leader=True)
    html = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_landing_survives_a_missing_leader_column(client, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("no such column: team_profile.is_leader")

    monkeypatch.setattr("app.team_profiles.landing.db.session.scalars", broken)
    response = client.get("/")
    assert response.status_code == 200
    assert FALLBACK_LEADERS[0]["name"] in response.get_data(as_text=True)


def test_leader_initials_and_icons():
    assert initials("Priya Raman") == "PR" and initials("Balaji") == "BA" and initials("") == "?"
    assert role_icon("VAPT Lead") == "target" and role_icon("Senior Developer") == "code"
    assert role_icon("Hardware lead") == "cpu" and role_icon("Volunteer") == "users"
