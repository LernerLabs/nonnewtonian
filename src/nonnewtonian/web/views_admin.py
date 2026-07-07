"""Admin queue (M3 slice): approve seeded communal entries onto the
public pages.  Token-gated by a single config value; the full moderation
UI and class-collection management arrive in M4+.

Constant-time token comparison; every mutating action is POST.
"""

from __future__ import annotations

import hmac

from flask import (
    Blueprint, abort, current_app, redirect, render_template, request, url_for,
)

from .. import collections_repo as repo
from . import queries as q

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
    return render_template(
        "admin.html", token=token, pending=pending_entries,
        approved_count=approved_count, shared=shared_entries,
    )


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
    row = conn.execute(
        "SELECT id FROM entries WHERE id=? AND collection_id IS NULL", (entry_id,)
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
