"""Team profile helpers: team IDs, linking to accounts, photos and social links."""

import re
import secrets
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from flask import current_app, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.team_profiles.models import STATUS_ACTIVE, STATUS_INVITED, TeamProfile
from app.urls import absolute_app_url
from app.utils import utcnow

TEAM_ID_PREFIX = "GMK-"
TEAM_ID_PATTERN = re.compile(r"^GMK-\d{4,6}$")
MAX_PHOTO_SIZE = 8 * 1024 * 1024
MAX_PHOTO_PIXELS = 30_000_000
PHOTO_SIZE = 640
PHOTO_FORMATS = {"JPEG", "PNG", "WEBP"}

NAME_LIMIT = 120
ROLE_LIMIT = 120
BIO_LIMIT = 400
URL_LIMIT = 300

# Each platform only accepts links on its own domain, so a profile can't
# present a look-alike "LinkedIn" button that points somewhere else.
SOCIAL_PLATFORMS = (
    {"key": "linkedin", "label": "LinkedIn", "icon": "linkedin", "domains": ("linkedin.com",)},
    {"key": "github", "label": "GitHub", "icon": "github", "domains": ("github.com",)},
    {"key": "x", "label": "X (Twitter)", "icon": "x", "domains": ("x.com", "twitter.com")},
    {"key": "instagram", "label": "Instagram", "icon": "instagram", "domains": ("instagram.com",)},
    {"key": "youtube", "label": "YouTube", "icon": "youtube", "domains": ("youtube.com", "youtu.be")},
    {"key": "tryhackme", "label": "TryHackMe", "icon": "flag", "domains": ("tryhackme.com",)},
    {"key": "hackthebox", "label": "Hack The Box", "icon": "terminal",
     "domains": ("hackthebox.com", "hackthebox.eu")},
    {"key": "website", "label": "Website", "icon": "globe", "domains": None},
)
SOCIAL_KEYS = {platform["key"] for platform in SOCIAL_PLATFORMS}


# ── Team IDs ────────────────────────────────────────────────────────────

def normalize_team_id(value):
    value = (value or "").strip().upper()
    return value if TEAM_ID_PATTERN.match(value) else None


def create_profile(organization_id, email, invited_by_id, name="", role_title=""):
    """Create an invited profile. Its team ID comes from the row's auto-increment
    id, which MariaDB never hands out twice, so a deleted ID is never reissued
    and an old shared link can't end up showing a different person."""
    profile = TeamProfile(
        organization_id=organization_id,
        team_id=f"TMP-{secrets.token_hex(6)}",
        email=email,
        name=name,
        role_title=role_title,
        bio="",
        socials={},
        invited_by_id=invited_by_id,
    )
    db.session.add(profile)
    try:
        db.session.flush()
        profile.team_id = f"{TEAM_ID_PREFIX}{profile.id:04d}"
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        raise ValueError("That email already has a team profile.") from error
    return profile


# ── Linking profiles to portal accounts ─────────────────────────────────

def link_profile(profile, user):
    """Attach a profile to a portal account when the member joins."""
    profile.user_id = user.id
    if profile.status == STATUS_INVITED:
        profile.status = STATUS_ACTIVE
    profile.joined_at = profile.joined_at or utcnow()
    if not profile.name:
        profile.name = user.name[:NAME_LIMIT]
    db.session.commit()
    return profile


def profile_for_user(user):
    """The member's own profile. Accounts that predate team profiles (such as the
    admin) get theirs the first time they open it, reusing a same-email profile."""
    profile = db.session.scalar(db.select(TeamProfile).where(TeamProfile.user_id == user.id))
    if profile is not None:
        return profile
    profile = db.session.scalar(
        db.select(TeamProfile).where(
            TeamProfile.organization_id == user.organization_id,
            func.lower(TeamProfile.email) == user.email.lower(),
            TeamProfile.user_id.is_(None),
        )
    )
    if profile is None:
        profile = create_profile(
            user.organization_id, user.email.lower(), user.id, name=user.name[:NAME_LIMIT]
        )
    return link_profile(profile, user)


def one_line(value, limit):
    """Collapse whitespace; None when the result is longer than limit."""
    value = " ".join((value or "").split())
    return value if len(value) <= limit else None


def is_public(profile):
    return profile.status == STATUS_ACTIVE and bool(profile.name and profile.role_title)


def public_profile_url(profile):
    try:
        return absolute_app_url("team_profiles.public_profile", team_id=profile.team_id)
    except RuntimeError:
        return url_for("team_profiles.public_profile", team_id=profile.team_id, _external=True)


def photo_url(profile, external=False):
    if not profile.photo_path:
        return None
    values = {"team_id": profile.team_id, "v": profile.photo_path[:8]}
    if external:
        try:
            return absolute_app_url("team_profiles.profile_photo", **values)
        except RuntimeError:
            return None
    return url_for("team_profiles.profile_photo", **values)


# ── Photos ──────────────────────────────────────────────────────────────

def _photo_root():
    root = Path(current_app.config["TEAM_PHOTO_STORAGE_PATH"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_photo(upload):
    """Validate, square-crop to 640px and re-encode as JPEG (drops EXIF/GPS data)."""
    data = upload.stream.read(MAX_PHOTO_SIZE + 1)
    if not data:
        raise ValueError("Choose a photo to upload.")
    if len(data) > MAX_PHOTO_SIZE:
        raise ValueError("Profile photos must be 8 MB or smaller.")
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in PHOTO_FORMATS:
                raise ValueError("Use a JPEG, PNG or WebP photo.")
            if source.width * source.height > MAX_PHOTO_PIXELS:
                raise ValueError("Profile photos must be 30 megapixels or smaller.")
            source.verify()
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                background = Image.new("RGB", image.size, (10, 14, 10))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image = ImageOps.fit(image, (PHOTO_SIZE, PHOTO_SIZE), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("The uploaded photo is not a valid image.") from error
    filename = f"{secrets.token_hex(24)}.jpg"
    path = _photo_root() / filename
    try:
        path.write_bytes(output.getvalue())
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return filename


def photo_file(filename):
    root = _photo_root()
    path = (root / filename).resolve()
    if root not in path.parents:
        raise RuntimeError("The team photo path escaped its storage directory.")
    return path


def remove_photo(filename):
    if filename:
        photo_file(filename).unlink(missing_ok=True)


# ── Social links ────────────────────────────────────────────────────────

def clean_social_url(value, platform):
    """Return a safe https URL for the platform, '' for blank, or raise ValueError."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > URL_LIMIT or any(ch.isspace() or ord(ch) < 32 for ch in value) or "\\" in value:
        raise ValueError(f"Enter a valid {platform['label']} link.")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as error:
        raise ValueError(f"Enter a valid {platform['label']} link.") from error
    if (
        parts.scheme.lower() != "https"
        or not host
        or "." not in host
        or parts.username
        or parts.password
        or port not in (None, 443)
    ):
        raise ValueError(f"{platform['label']} links must be https:// web addresses.")
    domains = platform["domains"]
    if domains and not any(host == domain or host.endswith("." + domain) for domain in domains):
        raise ValueError(
            f"{platform['label']} links must point to {' or '.join(domains)}."
        )
    return parts.geturl()


def social_links(profile):
    """Platforms with a saved link, in display order."""
    saved = profile.socials or {}
    return [
        {**platform, "url": saved[platform["key"]]}
        for platform in SOCIAL_PLATFORMS
        if saved.get(platform["key"])
    ]
