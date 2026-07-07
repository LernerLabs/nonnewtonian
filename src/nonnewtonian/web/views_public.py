"""Public, read-only pages — the browse-first communal site."""

from __future__ import annotations

import io
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, render_template, request, send_file, send_from_directory,
)

from ..slides import build_deck, build_entry_slide, DeckChapter
from . import queries as q

bp = Blueprint("public", __name__)


def _db():
    return current_app.get_db()


@bp.app_context_processor
def _inject_site():
    return {"site_name": current_app.config["SITE_NAME"]}


@bp.route("/")
def index():
    conn = _db()
    counts = {
        "scientists": len(q.all_approved_scientists(conn)),
        "textbooks": len(q.list_textbooks(conn)),
    }
    return render_template("index.html", counts=counts,
                           textbooks=q.list_textbooks(conn))


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
    return render_template("textbooks.html", textbooks=q.list_textbooks(_db()))


@bp.route("/textbooks/<slug>")
def textbook(slug):
    conn = _db()
    tb = q.textbook_by_slug(conn, slug)
    if not tb:
        abort(404)
    chapters = q.textbook_chapters(conn, tb["id"])
    placed = sum(len(c.entries) for c in chapters)
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
        _db().execute("SELECT 1")
    except Exception:  # pragma: no cover
        abort(503)
    return "ok", 200


# --- media & downloads ----------------------------------------------------

@bp.route("/photos/<path:relpath>")
def photo(relpath):
    """Serve a stored photo.  In production Caddy serves this tree
    directly; the route is the dev/local fallback."""
    photo_dir = Path(current_app.config["PHOTO_DIR"]).resolve()
    return send_from_directory(photo_dir, relpath)


def _entry_image_paths(entry: q.DisplayEntry) -> list[Path]:
    photo_dir = Path(current_app.config["PHOTO_DIR"]).resolve()
    paths = []
    for photo_row in entry.photos:
        if photo_row.file_path:
            paths.append(photo_dir / photo_row.file_path)
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
    tb = q.textbook_by_slug(conn, slug)
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
