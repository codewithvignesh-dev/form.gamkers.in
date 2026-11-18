from app.auth.models import Invitation, PasswordReset, Role, RolePermission, User
from app.certificates.models import CertificateBatch, CertificateRecord, CertificateUpload
from app.extensions import db, login_manager
from app.forms.models import Form, Submission
from app.organization.models import Organization
from app.team_profiles.models import TeamProfile
from app.utils import utcnow


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
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
