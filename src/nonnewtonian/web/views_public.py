"""Public, read-only pages — the browse-first communal site."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import threading
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, make_response, render_template, request, send_file,
    send_from_directory,
)

from ..slides import build_deck, build_entry_slide, DeckChapter
from . import queries as q

bp = Blueprint("public", __name__)

LOW_DISK_BYTES = 256 * 1024 ** 2
_HEAVY_BUILD = threading.BoundedSemaphore(1)   # whole-book/class decks; 512MB box
HEAVY_BUILD_WAIT_SECONDS = 20


@contextlib.contextmanager
def heavy_build():
    """At most one whole-deck build at a time. Correct only because gunicorn
    runs ONE process (deploy/gunicorn.conf.py)."""
    # abort() here raises before the try: nothing was acquired, so nothing
    # is released (and a BoundedSemaphore would raise on an extra release).
    if not _HEAVY_BUILD.acquire(timeout=HEAVY_BUILD_WAIT_SECONDS):
        abort(make_response("The deck builder is busy; please try again in a minute.",
                            503, {"Retry-After": "30"}))
    try:
        yield
    finally:
        _HEAVY_BUILD.release()


def _db():
    return current_app.get_db()


@bp.app_context_processor
def _inject_site():
    return {"site_name": current_app.config["SITE_NAME"]}


@bp.route("/")
def index():
    conn = _db()
    public_tbs = q.public_textbooks(conn)
    counts = {
        "scientists": len(q.all_approved_scientists(conn)),
        "textbooks": len(public_tbs),
    }
    return render_template("index.html", counts=counts, textbooks=public_tbs)


@bp.route("/scientists")
def scientists():
    search = request.args.get("q", "").strip() or None
    entries = q.all_approved_scientists(_db(), search=search)
    return render_template("scientists.html", entries=entries, search=search or "")


@bp.route("/scientists/<slug>")
def scientist(slug):
    writeups = q.approved_entries_by_slug(_db(), slug)
    if not writeups:
        abort(404)
    return render_template("scientist.html", writeups=writeups, name=writeups[0].name)


@bp.route("/textbooks")
def textbooks():
    return render_template("textbooks.html", textbooks=q.public_textbooks(_db()))


@bp.route("/textbooks/<slug>")
def textbook(slug):
    conn = _db()
    tb = q.public_textbook_by_slug(conn, slug)
    if not tb:
        abort(404)
    chapters = q.textbook_chapters(conn, tb["id"])
    # Count DISTINCT scientists, not placement cards: a scientist placed in
    # two chapters must not inflate the total (it read "52 scientists" when
    # only 39 exist). The whole-book deck / chapter view still show every
    # placement; this header just states how many people are on the page.
    placed = len({e.slug for c in chapters for e in c.entries})
    return render_template("textbook.html", textbook=tb, chapters=chapters, placed=placed)


@bp.route("/wanted")
def wanted():
    return render_template("wanted.html", wanted=q.wanted_scientists(_db()))


@bp.route("/about")
def about():
    return render_template("about.html")


@bp.route("/for-teachers")
def for_teachers():
    return render_template("for_teachers.html")


@bp.route("/for-students")
def for_students():
    return render_template("for_students.html")


@bp.route("/privacy")
def privacy():
    return render_template("privacy.html")


@bp.route("/healthz")
def healthz():
    try:
        # Touch a real table so a missing/half-applied schema fails the
        # check instead of reporting healthy (M3 review).
        _db().execute("SELECT 1 FROM entries LIMIT 1")
    except Exception:  # pragma: no cover
        abort(503)
    photo_dir = Path(current_app.config["PHOTO_DIR"])
    if not os.access(photo_dir, os.W_OK):
        return "photo dir not writable", 503
    if shutil.disk_usage(photo_dir).free < LOW_DISK_BYTES:
        return "low disk", 503
    return "ok", 200


# --- media & downloads ----------------------------------------------------

@bp.route("/photos/<path:relpath>")
def photo(relpath):
    """Serve a stored photo.  In production Caddy serves this tree directly
    (deploy/Caddyfile); this route is the dev fallback.  Neither checks
    approval status: a pending or rejected photo is reachable by anyone
    holding its content-hash URL."""
    photo_dir = Path(current_app.config["PHOTO_DIR"]).resolve()
    return send_from_directory(photo_dir, relpath)


def _entry_image_paths(entry: q.DisplayEntry) -> list[Path]:
    photo_dir = Path(current_app.config["PHOTO_DIR"]).resolve()
    paths = []
    for photo_row in entry.photos:
        if not photo_row.file_path:
            continue
        # file_path is always a content-hash relative path today, but keep
        # slide generation inside PHOTO_DIR defensively (absolute or ../
        # paths would otherwise escape via Path join). Skip anything that
        # resolves outside the tree rather than embedding it in a .pptx.
        candidate = (photo_dir / photo_row.file_path).resolve()
        if candidate == photo_dir or photo_dir in candidate.parents:
            paths.append(candidate)
    return paths


@bp.route("/scientists/<slug>/slide.pptx")
def scientist_slide(slug):
    writeups = q.approved_entries_by_slug(_db(), slug)
    if not writeups:
        abort(404)
    entry = writeups[0]
    prs = build_entry_slide(entry.to_parser_entry(), _entry_image_paths(entry))
    return _send_pptx(prs, f"{slug}.pptx")


@bp.route("/textbooks/<slug>/deck.pptx")
def textbook_deck(slug):
    conn = _db()
    tb = q.public_textbook_by_slug(conn, slug)
    if not tb:
        abort(404)
    chapters = q.textbook_chapters(conn, tb["id"])
    deck_chapters = [
        DeckChapter(
            chapter=c.chapter, topics=c.topics,
            entries=[(e.to_parser_entry(), _entry_image_paths(e)) for e in c.entries],
        )
        for c in chapters if c.entries
    ]
    # The save into the in-memory buffer is as heavy as the build, so both
    # sit inside the one-at-a-time slot.
    with heavy_build():
        prs = build_deck(deck_chapters, deck_title=tb["title"])
        return _send_pptx(prs, f"{slug}-deck.pptx")


def _send_pptx(prs, filename):
    buffer = io.BytesIO()
    prs.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer, as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
