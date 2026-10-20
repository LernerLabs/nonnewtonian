"""Admin queue (M3 slice): approve seeded communal entries onto the
public pages.  Token-gated by a single config value; the full moderation
UI and class-collection management arrive in M4+.

Constant-time token comparison; every mutating action is POST.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, redirect, render_template, request, url_for,
)

from .. import collections_repo as repo
from . import queries as q
from .views_manage import _abs, _safe_unlink, _self_mailto

bp = Blueprint("admin", __name__)


def _check_token(token: str) -> None:
    expected = current_app.config["ADMIN_TOKEN"]
    # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII
    # str input, which would 500 (an existence oracle) instead of 404
    # (M3 review, confirmed). utf-8 bytes compare safely and constant-time.
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        abort(404)  # do not reveal that the path exists


def _db():
    return current_app.get_db()


@bp.route("/admin/<token>")
def dashboard(token):
    _check_token(token)
    conn = _db()
    pending = conn.execute(
        "SELECT * FROM entries WHERE collection_id IS NULL "
        "AND communal_status='pending' ORDER BY scientist_name COLLATE NOCASE"
    ).fetchall()
    pending_entries = [q._row_to_entry(conn, r) for r in pending]
    approved_count = conn.execute(
        "SELECT count(*) FROM entries WHERE collection_id IS NULL "
        "AND communal_status='approved'"
    ).fetchone()[0]
    # M5: entries a class chose to share to the communal site, awaiting
    # your review before they're cloned onto the public pages.
    shared = conn.execute(
        "SELECT e.*, c.name AS class_name FROM entries e "
        "JOIN collections c ON e.collection_id = c.id "
        "WHERE e.share_communal=1 AND e.communal_status='pending' "
        "ORDER BY e.scientist_name COLLATE NOCASE"
    ).fetchall()
    shared_entries = [(q._row_to_entry(conn, r), r["class_name"]) for r in shared]
    published = [q._row_to_entry(conn, r) for r in conn.execute(
        "SELECT * FROM entries WHERE collection_id IS NULL AND communal_status='approved' "
        "ORDER BY scientist_name COLLATE NOCASE")]
    classes = conn.execute(
        "SELECT c.id, c.name, c.slug, c.teacher_email, c.created_at, c.manage_token_hash, "
        "(SELECT count(*) FROM entries e WHERE e.collection_id=c.id) AS n "
        "FROM collections c ORDER BY c.created_at DESC").fetchall()
    return render_template(
        "admin.html", token=token, pending=pending_entries,
        approved_count=approved_count, shared=shared_entries, published=published,
        classes=classes, took_down=request.args.get("took_down", type=int),
        reissue_stale=request.args.get("reissue_stale", type=int),
    )


def _delete_entry_and_orphan_photos(conn, entry_id: int) -> None:
    """Delete one entry (cascading its placements/photos rows) and unlink
    any photo file no surviving row references. Photos are content-
    addressed and shared (a communal clone points at its source's file),
    so the refcount runs AFTER the DELETE."""
    photo_dir = Path(current_app.config["PHOTO_DIR"]).resolve()
    paths = {r["file_path"] for r in conn.execute(
        "SELECT file_path FROM photos WHERE entry_id=? AND file_path IS NOT NULL", (entry_id,))}
    conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    for p in paths:
        if not conn.execute("SELECT 1 FROM photos WHERE file_path=? LIMIT 1", (p,)).fetchone():
            _safe_unlink(photo_dir, p)
    conn.commit()


@bp.route("/admin/<token>/communal/<int:entry_id>/delete", methods=["POST"])
def delete_communal(token, entry_id):
    """Take down a published communal entry (seed or promoted clone) — the
    remedy for a later copyright/privacy complaint (M5 review). Removes
    the entry (cascading its placements/photos) and refcount-unlinks any
    photo file nothing else references."""
    _check_token(token)
    conn = _db()
    row = conn.execute(
        "SELECT id FROM entries WHERE id=? AND collection_id IS NULL", (entry_id,)
    ).fetchone()
    if not row:
        abort(404)
    _delete_entry_and_orphan_photos(conn, entry_id)
    return redirect(url_for("admin.dashboard", token=token))


@bp.route("/admin/<token>/takedown", methods=["POST"])
def takedown(token):
    """Take down ANY entry by id, class or communal (M7). The id is in
    every slide URL (/c/<slug>/entry/<id>/slide.pptx), so a complaint
    about a student's name on a class page no longer needs hand SQL.
    A communal clone is a separate entry: taking down a class entry does
    not remove its public copy (take that down by its own id)."""
    _check_token(token)
    entry_id = request.form.get("entry_id", type=int)
    conn = _db()
    if entry_id is None or not conn.execute(
            "SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone():
        abort(404)
    _delete_entry_and_orphan_photos(conn, entry_id)
    return redirect(url_for("admin.dashboard", token=token, took_down=entry_id))


@bp.route("/admin/<token>/collections/<int:cid>/reissue", methods=["POST"])
def reissue(token, cid):
    """Mint a fresh manage link for a class whose teacher lost theirs, with
    no SMTP needed (M7). The old link dies at once. The new token is shown
    once on the response page and never logged or stored in readable form.

    The form carries the hash prefix it was rendered with, and the swap is
    a compare-and-swap: a reload or Back+resubmit of the show-once page
    (served no-store, so the browser re-POSTs) gets a "stale" banner
    instead of silently killing the link just sent."""
    _check_token(token)
    conn = _db()
    coll = conn.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone()
    if coll is None:
        abort(404)
    seen = request.form.get("expected", "")
    t = None
    if len(seen) == 16 and coll["manage_token_hash"].startswith(seen):
        t = repo.reissue_manage_token(conn, cid, expected_hash=coll["manage_token_hash"])
    if t is None:  # stale form, or another re-issue won the race
        return redirect(url_for("admin.dashboard", token=token, reissue_stale=cid))
    manage_url = _abs(url_for("manage.dashboard", token=t))
    welcome_url = _abs(url_for("manage.welcome", token=t))
    student_url = _abs(url_for("cls.submit", slug=coll["slug"]))
    return render_template(
        "admin_reissued.html", token=token, coll=coll, manage_url=manage_url,
        welcome_url=welcome_url, teacher_email=coll["teacher_email"],
        mailto=_self_mailto(coll, student_url, manage_url))


@bp.route("/admin/<token>/shared/<int:entry_id>/<action>", methods=["POST"])
def moderate_shared(token, entry_id, action):
    """Approve/reject a class-shared entry for the communal site (M5).
    Approve clones it into an independent communal entry."""
    _check_token(token)
    if action not in ("approve", "reject"):
        abort(400)
    conn = _db()
    row = conn.execute(
        "SELECT id FROM entries WHERE id=? AND share_communal=1 "
        "AND communal_status='pending' AND collection_id IS NOT NULL", (entry_id,)
    ).fetchone()
    if not row:
        abort(404)
    now = current_app.utcnow()
    if action == "approve":
        repo.clone_to_communal(conn, entry_id, now)
        conn.execute(
            "UPDATE entries SET communal_status='approved', updated_at=? WHERE id=?",
            (now, entry_id))
    else:
        conn.execute(
            "UPDATE entries SET communal_status='rejected', updated_at=? WHERE id=?",
            (now, entry_id))
    conn.commit()
    return redirect(url_for("admin.dashboard", token=token))


@bp.route("/admin/<token>/entry/<int:entry_id>/<action>", methods=["POST"])
def moderate(token, entry_id, action):
    _check_token(token)
    if action not in ("approve", "reject"):
        abort(400)
    conn = _db()
    # Only act on entries still awaiting communal review. Without the
    # communal_status='pending' guard this route also matched already-
    # approved communal CLONES (also collection_id IS NULL), letting a
    # reject strand a clone in an unrecoverable state (M5 review). Taking
    # down an already-published entry goes through delete_communal.
    row = conn.execute(
        "SELECT id FROM entries WHERE id=? AND collection_id IS NULL "
        "AND communal_status='pending'", (entry_id,)
    ).fetchone()
    if not row:
        abort(404)
    now = current_app.utcnow()
    if action == "approve":
        conn.execute(
            "UPDATE entries SET status='approved', communal_status='approved', "
            "approved_at=?, updated_at=? WHERE id=?",
            (now, now, entry_id),
        )
    else:
        conn.execute(
            "UPDATE entries SET communal_status='rejected', updated_at=? WHERE id=?",
            (now, entry_id),
        )
    conn.commit()
    return redirect(url_for("admin.dashboard", token=token))


@bp.route("/admin/<token>/approve-all-seeds", methods=["POST"])
def approve_all_seeds(token):
    """Bulk-approve the seeded corpus (a convenience for standing up the
    communal site; each seed was still individually reviewable above)."""
    _check_token(token)
    conn = _db()
    now = current_app.utcnow()
    conn.execute(
        "UPDATE entries SET status='approved', communal_status='approved', "
        "approved_at=?, updated_at=? "
        "WHERE seed_origin IS NOT NULL AND communal_status='pending'",
        (now, now),
    )
    conn.commit()
    return redirect(url_for("admin.dashboard", token=token))
