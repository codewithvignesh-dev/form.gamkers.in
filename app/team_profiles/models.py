from app.extensions import db
from app.utils import utcnow

STATUS_INVITED = "invited"
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"


class TeamProfile(db.Model):
    """A GAMKERS team member's public profile and team ID.

    Created with the portal invitation (/settings/team) and linked to the member's
    user account once they accept it. Profiles from before that link have no user.
    """

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer, db.ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id = db.Column(db.String(16), nullable=False, unique=True, index=True)
    email = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120), nullable=False, default="")
    role_title = db.Column(db.String(120), nullable=False, default="")
    bio = db.Column(db.Text, nullable=False, default="")
    photo_path = db.Column(db.String(80))
    socials = db.Column(db.JSON, nullable=False, default=dict)
    status = db.Column(db.String(16), nullable=False, default=STATUS_INVITED)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), unique=True)
    invited_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    joined_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        db.UniqueConstraint("organization_id", "email", name="uq_team_profile_email"),
        # Team IDs come from this id; never reuse one (MariaDB already doesn't).
        {"sqlite_autoincrement": True},
    )

    @property
    def is_active(self):
        return self.status == STATUS_ACTIVE

    @property
    def display_name(self):
        return self.name or self.email.split("@")[0]

