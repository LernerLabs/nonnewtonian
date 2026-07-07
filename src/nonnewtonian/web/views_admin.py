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

from . import queries as q

bp = Blueprint("admin", __name__)


def _check_token(token: str) -> None:
    expected = current_app.config["ADMIN_TOKEN"]
    if not hmac.compare_digest(token, expected):
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
        "SELECT count(*) FROM entries WHERE communal_status='approved'"
    ).fetchone()[0]
    return render_template(
        "admin.html", token=token, pending=pending_entries,
        approved_count=approved_count,
    )


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
