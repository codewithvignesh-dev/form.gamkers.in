from app.auth.models import Invitation, PasswordReset, Role, RolePermission, User
from app.certificates.models import CertificateBatch, CertificateRecord, CertificateUpload
from app.extensions import db, login_manager
from app.forms.models import Form, Submission
from app.organization.models import Organization
from app.resilience import DATABASE_ERRORS, database_down, mark_database_down
from app.team_profiles.models import TeamProfile
from app.utils import utcnow


@login_manager.user_loader
def load_user(user_id):
    if database_down():
        return None
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    except DATABASE_ERRORS as error:
        # Public pages stay usable (as signed out) while the database is down.
        # The session cookie is left alone, so the member is still signed in
        # as soon as the database is back.
        mark_database_down(error)
        return None


__all__ = [
    "CertificateBatch",
    "CertificateRecord",
    "CertificateUpload",
    "Form",
    "Invitation",
    "Organization",
    "PasswordReset",
    "Role",
    "RolePermission",
    "Submission",
    "TeamProfile",
    "User",
    "utcnow",
]
