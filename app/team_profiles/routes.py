"""GAMKERS team profiles.

- Members edit their own profile at /settings/profile with their normal sign-in.
- Admins create profiles by inviting members at /settings/team (organization blueprint).
- Public (/team/<team ID>): the shareable, verifiable team profile page.
"""

from flask import (
    Blueprint,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.team_profiles.models import STATUS_SUSPENDED, TeamProfile
from app.team_profiles.services import (
    BIO_LIMIT,
    NAME_LIMIT,
    ROLE_LIMIT,
    SOCIAL_PLATFORMS,
    clean_social_url,
    is_public,
    normalize_team_id,
    one_line,
    photo_file,
    photo_url,
    profile_for_user,
    public_profile_url,
    remove_photo,
    save_photo,
    social_links,
)

team_profiles = Blueprint("team_profiles", __name__)


def _is_owner(profile):
    return current_user.is_authenticated and profile.user_id == current_user.id


# ── Members: edit your own profile ──────────────────────────────────────

@team_profiles.route("/settings/profile", methods=["GET", "POST"])
@login_required
def my_profile():
    try:
        profile = profile_for_user(current_user)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("main.dashboard"))
    values = {
        "name": profile.name,
        "role_title": profile.role_title,
        "bio": profile.bio,
        **{platform["key"]: (profile.socials or {}).get(platform["key"], "") for platform in SOCIAL_PLATFORMS},
    }
    if request.method == "POST":
        values.update({key: request.form.get(key, "") for key in values})
        errors = []
        name = one_line(values["name"], NAME_LIMIT)
        role_title = one_line(values["role_title"], ROLE_LIMIT)
        bio = (values["bio"] or "").strip().replace("\r\n", "\n")
        if not name:
            errors.append("Enter your name (up to 120 characters).")
        if not role_title:
            errors.append("Enter your role in the team (up to 120 characters).")
        if len(bio) > BIO_LIMIT:
            errors.append(f"Keep your bio under {BIO_LIMIT} characters.")
        socials = {}
        for platform in SOCIAL_PLATFORMS:
            try:
                url = clean_social_url(values[platform["key"]], platform)
            except ValueError as error:
                errors.append(str(error))
                continue
            if url:
                socials[platform["key"]] = url
        new_photo = None
        upload = request.files.get("photo")
        if not errors and upload and upload.filename:
            try:
                new_photo = save_photo(upload)
            except ValueError as error:
                errors.append(str(error))
        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "team_profiles/my_profile.html", profile=profile, values=values, **_editor_context(profile)
            ), 400
        old_photo = profile.photo_path
        profile.name, profile.role_title, profile.bio = name, role_title, bio
        profile.socials = socials
        if new_photo:
            profile.photo_path = new_photo
        elif request.form.get("remove_photo") == "yes":
            profile.photo_path = None
        db.session.commit()
        if old_photo and old_photo != profile.photo_path:
            remove_photo(old_photo)
        flash("Profile saved. Your public page is up to date.", "success")
        return redirect(url_for("team_profiles.my_profile"))
    return render_template(
        "team_profiles/my_profile.html", profile=profile, values=values, **_editor_context(profile)
    )


def _editor_context(profile):
    return {
        "platforms": SOCIAL_PLATFORMS,
        "public_url": public_profile_url(profile),
        "photo_url": photo_url(profile),
        "is_public": is_public(profile),
        "bio_limit": BIO_LIMIT,
    }


# ── Public: shareable profile by team ID ────────────────────────────────

@team_profiles.get("/team")
def lookup():
    raw = request.args.get("team_id", "")
    if raw:
        team_id = normalize_team_id(raw)
        if team_id:
            return redirect(url_for("team_profiles.public_profile", team_id=team_id))
        flash("Team IDs look like GMK-0001.", "error")
    return render_template("team_profiles/lookup.html", query=raw)


@team_profiles.get("/team/<team_id>")
def public_profile(team_id):
    response = make_response(_render_public_profile(team_id))
    # Verification pages must never be served stale: a suspended ID has to stop
    # looking "verified" for everyone straight away.
    response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


def _render_public_profile(team_id):
    normalized = normalize_team_id(team_id)
    if normalized and normalized != team_id:
        return redirect(url_for("team_profiles.public_profile", team_id=normalized), 301)
    profile = (
        db.session.scalar(db.select(TeamProfile).where(TeamProfile.team_id == normalized))
        if normalized
        else None
    )
    if profile is None or (profile.status != STATUS_SUSPENDED and not is_public(profile)):
        return render_template("team_profiles/lookup.html", query=team_id, not_found=True), 404
    if profile.status == STATUS_SUSPENDED:
        return render_template("team_profiles/inactive.html", team_id=profile.team_id), 410
    return render_template(
        "team_profiles/public.html",
        profile=profile,
        links=social_links(profile),
        photo_url=photo_url(profile),
        og_image=photo_url(profile, external=True),
        public_url=public_profile_url(profile),
        is_owner=_is_owner(profile),
    )


@team_profiles.get("/team/<team_id>/photo")
def profile_photo(team_id):
    profile = db.session.scalar(
        db.select(TeamProfile).where(TeamProfile.team_id == normalize_team_id(team_id))
    )
    if profile is None or not profile.photo_path:
        abort(404)
    is_manager = current_user.is_authenticated and current_user.has_permission(
        "team_profiles.manage"
    )
    if not (is_public(profile) or _is_owner(profile) or is_manager):
        abort(404)
    path = photo_file(profile.photo_path)
    if not path.is_file():
        abort(404)
    response = send_file(path, mimetype="image/jpeg", max_age=86400)
    if not is_public(profile):
        response.cache_control.public = False
        response.cache_control.private = True
    return response


# Old /id addresses (shared before profiles moved to /team) redirect permanently.
@team_profiles.get("/id", endpoint="legacy_lookup")
@team_profiles.get("/id/<team_id>", endpoint="legacy_profile")
@team_profiles.get("/id/<team_id>/photo", endpoint="legacy_photo")
def legacy_id_redirect(team_id=None):
    path = request.path.replace("/id", "/team", 1)
    query = request.query_string.decode("utf-8", "ignore")
    return redirect(path + (f"?{query}" if query else ""), 301)
