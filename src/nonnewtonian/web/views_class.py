"""Class-facing pages: the public class collection, the student
submission form, and downloads.  Blueprint name 'cls' ('class' is a
Python keyword)."""

from __future__ import annotations

import time
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, render_template, request,
)

from .. import collections_repo as repo
from ..photos import PhotoError, fetch_photo, store_bytes
from ..slugs import slugify
from . import queries as q
from . import limiter
from .views_public import _entry_image_paths, _send_pptx
from ..slides import DeckChapter, build_deck, build_entry_slide

bp = Blueprint("cls", __name__)

MIN_SUBMIT_SECONDS = 3  # a human takes longer than this to fill the form


def _db():
    return current_app.get_db()


def _collection_or_404(slug):
    row = repo.collection_by_slug(_db(), slug)
    if not row:
        abort(404)
    return row


@bp.route("/c/<slug>")
def page(slug):
    coll = _collection_or_404(slug)
    conn = _db()
    chapters = q.class_chapters(conn, coll["id"], coll["textbook_id"]) if coll["textbook_id"] else []
    textbook = conn.execute("SELECT * FROM textbooks WHERE id=?", (coll["textbook_id"],)).fetchone() if coll["textbook_id"] else None
    placed = sum(len(c.entries) for c in chapters)
    return render_template("class_page.html", coll=coll, textbook=textbook,
                           chapters=chapters, placed=placed)


@bp.route("/c/<slug>/embed")
def embed(slug):
    coll = _collection_or_404(slug)
    conn = _db()
    chapters = q.class_chapters(conn, coll["id"], coll["textbook_id"]) if coll["textbook_id"] else []
    resp = current_app.make_response(render_template("embed.html", coll=coll, chapters=chapters))
    # Embed routes are the ONE framable surface; the global after_request
    # (app factory) sets X-Frame-Options: DENY everywhere a CSP
    # frame-ancestors isn't already present, so this line is what keeps
    # the embed framable while manage/moderation pages stay frame-denied.
    resp.headers["Content-Security-Policy"] = "frame-ancestors *"
    return resp


@bp.route("/c/<slug>/submit")
def submit(slug):
    coll = _collection_or_404(slug)
    conn = _db()
    toc = []
    if coll["textbook_id"]:
        toc = [dict(r) for r in conn.execute(
            "SELECT DISTINCT chapter, topics FROM toc_rows WHERE textbook_id=? ORDER BY sort_order",
            (coll["textbook_id"],))]
    modes = repo.allowed_attribution_modes(coll["max_attribution"])
    return render_template("submit.html", coll=coll, toc=toc, modes=modes,
                           form_ts=int(time.time()), error=None, values={})


@bp.route("/c/<slug>/submit", methods=["POST"])
@limiter.limit("20 per hour")
def submit_post(slug):
    coll = _collection_or_404(slug)
    conn = _db()

    if not coll["submissions_open"]:
        abort(403)
    # Spam traps: honeypot + minimum fill time.
    if request.form.get("website"):  # hidden honeypot field
        abort(400)
    try:
        elapsed = time.time() - int(request.form.get("form_ts", "0"))
    except ValueError:
        elapsed = 0
    if elapsed < MIN_SUBMIT_SECONDS:
        abort(400)

    values = request.form
    name = values.get("scientist_name", "").strip()
    description = values.get("description", "").strip()
    error = _validate_submission(conn, coll, name, description, values)
    if error:
        return _rerender_submit(coll, values, error), 400

    placement = _build_placement(conn, coll, values)
    # If the class has a textbook, an entry with no resolved chapter would
    # be approved yet never appear on the chapter page (silent loss, M4
    # review). Require a valid chapter rather than accept an invisible one.
    if coll["textbook_id"] and placement is None:
        return _rerender_submit(coll, values,
                                "Please choose a chapter from the list."), 400
    entry_id = repo.create_submission(
        conn, collection_id=coll["id"], scientist_name=name,
        description=description, sources_text=values.get("sources", "").strip(),
        why_chapter=values.get("why_chapter", "").strip() or None,
        contributor_name=values.get("contributor_name", "").strip() or None,
        attribution_mode=_capped_mode(coll, values.get("attribution_mode", "anonymous")),
        license_grant=bool(values.get("license_grant")),
        now=current_app.utcnow(), placement=placement)

    photo_error = _attach_photo(conn, coll, entry_id, values, request.files)
    return render_template("submit_thanks.html", coll=coll, name=name,
                           photo_warning=photo_error)


def _validate_submission(conn, coll, name, description, values) -> str | None:
    if not name:
        return "Please enter the scientist's name."
    if not description:
        return "Please write a short description."
    if repo.duplicate_scientist(conn, coll["id"], name):
        return (f"Someone in your class has already submitted {name}. "
                "Try choosing a different scientist.")
    if not values.get("license_grant"):
        return ("Please check the box granting permission to publish, so your "
                "teacher can share your work.")
    return None


def _capped_mode(coll, requested: str) -> str:
    allowed = repo.allowed_attribution_modes(coll["max_attribution"])
    return requested if requested in allowed else coll["max_attribution"]


def _build_placement(conn, coll, values) -> dict | None:
    if not coll["textbook_id"]:
        return None
    raw_chapter = values.get("chapter", "").strip()
    if not raw_chapter:
        return None
    try:
        chapter = int(raw_chapter)
    except ValueError:
        return None
    toc_row = conn.execute(
        "SELECT id, topics FROM toc_rows WHERE textbook_id=? AND chapter=? ORDER BY sort_order LIMIT 1",
        (coll["textbook_id"], chapter)).fetchone()
    if not toc_row:
        return None
    return {
        "textbook_id": coll["textbook_id"], "toc_row_id": toc_row["id"],
        "chapter": chapter, "section_label": values.get("section", "").strip() or None,
        "raw_line": f"Chapter {chapter}: {toc_row['topics']}",
    }


def _attach_photo(conn, coll, entry_id, values, files) -> str | None:
    """Store an uploaded file or fetched URL; returns a warning string on
    failure (the entry still submits — a photo is optional)."""
    photo_dir = Path(current_app.config["PHOTO_DIR"]).resolve()
    upload = files.get("photo_file")
    url = values.get("photo_url", "").strip()
    stored = None
    try:
        if upload and upload.filename and coll["allow_photo_upload"]:
            stored = store_bytes(upload.read(), photo_dir)
        elif url:
            stored = fetch_photo(url, photo_dir)
    except PhotoError as exc:
        return str(exc)
    if not stored:
        return None
    conn.execute(
        "INSERT INTO photos(entry_id,original_url,file_path,sha256,content_type,"
        "width,height,is_primary,fetch_status,attribution,license,license_verified) "
        "VALUES(?,?,?,?,?,?,?,1,'stored',?,?,0)",
        (entry_id, url or None, str(stored.path.relative_to(photo_dir)), stored.sha256,
         stored.content_type, stored.width, stored.height,
         values.get("photo_attribution", "").strip() or url or None,
         values.get("photo_license", "").strip() or "source unverified"))
    conn.commit()
    return None


def _rerender_submit(coll, values, error):
    conn = _db()
    toc = []
    if coll["textbook_id"]:
        toc = [dict(r) for r in conn.execute(
            "SELECT DISTINCT chapter, topics FROM toc_rows WHERE textbook_id=? ORDER BY sort_order",
            (coll["textbook_id"],))]
    return render_template("submit.html", coll=coll, toc=toc,
                           modes=repo.allowed_attribution_modes(coll["max_attribution"]),
                           form_ts=int(time.time()), error=error, values=values)


@bp.route("/c/<slug>/entry/<int:entry_id>/slide.pptx")
def entry_slide(slug, entry_id):
    coll = _collection_or_404(slug)
    entry = q.approved_class_entry(_db(), coll["id"], entry_id)
    if not entry:
        abort(404)
    prs = build_entry_slide(entry.to_parser_entry(), _entry_image_paths(entry))
    return _send_pptx(prs, f"{entry.slug}.pptx")


@bp.route("/c/<slug>/deck.pptx")
def deck(slug):
    coll = _collection_or_404(slug)
    conn = _db()
    if not coll["textbook_id"]:
        abort(404)
    chapters = q.class_chapters(conn, coll["id"], coll["textbook_id"])
    deck_chapters = [
        DeckChapter(chapter=c.chapter, topics=c.topics,
                    entries=[(e.to_parser_entry(), _entry_image_paths(e)) for e in c.entries])
        for c in chapters if c.entries]
    prs = build_deck(deck_chapters, deck_title=coll["name"])
    return _send_pptx(prs, f"{slug}-deck.pptx")
