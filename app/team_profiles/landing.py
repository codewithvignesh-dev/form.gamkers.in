"""The team section of the public landing page.

It is built from the team profiles members maintain, and must never be the reason
the landing page fails. Leader cards fall back to the built-in list below when the
database is down or no leader is set. Contributors are only ever read from the
database (portal members with the Contributor role), so they are simply left out
of the page when there are none.
"""

from flask import current_app

from app.auth.models import User
from app.extensions import db
from app.resilience import DATABASE_ERRORS, database_down, mark_database_down
from app.team_profiles.models import STATUS_ACTIVE, TeamProfile
from app.team_profiles.services import photo_url

FALLBACK_LEADERS = (
    {"initials": "AM", "name": "Akash M.", "role": "Cybersecurity Tech Lead", "icon": "shield"},
    {"initials": "BJ", "name": "Balaji", "role": "VAPT Lead", "icon": "target"},
    {"initials": "VW", "name": "Vigneshwaran", "role": "Senior Developer", "icon": "code"},
)

# Only members holding this portal role are listed in the contributors strip.
CONTRIBUTOR_ROLE = "contributor"

# First matching keyword wins; the icons are the ones the landing page already ships.
_ROLE_ICONS = (
    (("vapt", "red team", "pentest", "penetration", "offensive"), "target"),
    (("develop", "engineer", "programmer", "software", "code"), "code"),
    (("hardware", "iot", "embedded", "firmware"), "cpu"),
    (("research",), "search"),
    (("educat", "trainer", "mentor", "content"), "book"),
    (("secur", "cyber", "lead", "head", "founder"), "shield"),
)


def role_icon(role_title):
    lowered = (role_title or "").lower()
    for keywords, icon in _ROLE_ICONS:
        if any(keyword in lowered for keyword in keywords):
            return icon
    return "users"


def initials(name):
    words = [word for word in (name or "").split() if word[:1].isalnum()]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (name or "?").strip()[:2].upper() or "?"


def _fallback():
    return {
        "leaders": [dict(item, team_id=None, photo=None) for item in FALLBACK_LEADERS],
        "contributors": [],
        "from_profiles": False,
    }


def landing_team():
    """Leaders and contributors for the landing page: plain dicts, safe to render
    after the session is gone. Each group falls back on its own."""
    fallback = _fallback()
    if database_down():
        return fallback
    try:
        rows = db.session.execute(
            db.select(TeamProfile, User.role)
            .outerjoin(User, User.id == TeamProfile.user_id)
            .where(
                TeamProfile.status == STATUS_ACTIVE,
                TeamProfile.name != "",
                TeamProfile.role_title != "",
            )
            .order_by(TeamProfile.id)
        ).all()
        listed = [
            {
                "team_id": profile.team_id,
                "name": profile.name,
                "role": profile.role_title,
                "is_leader": profile.is_leader,
                "portal_role": portal_role,
                # None when no photo was uploaded: the page then shows initials.
                "photo": photo_url(profile),
            }
            for profile, portal_role in rows
        ]
    except DATABASE_ERRORS as error:
        mark_database_down(error)
        return fallback
    except Exception:
        # Anything unexpected here must not take the whole landing page down.
        current_app.logger.exception("Could not load team profiles for the landing page.")
        db.session.rollback()
        return fallback
    leaders = [
        {
            "initials": initials(item["name"]),
            "name": item["name"],
            "role": item["role"],
            "icon": role_icon(item["role"]),
            "team_id": item["team_id"],
            "photo": item["photo"],
        }
        for item in listed
        if item["is_leader"]
    ]
    contributors = [
        {"name": item["name"], "team_id": item["team_id"], "photo": item["photo"]}
        for item in listed
        if item["portal_role"] == CONTRIBUTOR_ROLE and not item["is_leader"]
    ]
    return {
        "leaders": leaders or fallback["leaders"],
        "contributors": contributors,
        "from_profiles": True,
    }
