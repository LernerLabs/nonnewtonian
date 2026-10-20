"""Optional email recovery of a lost management link (M7 WP12).

Only live when all four SMTP settings are present (SMTP_CONFIGURED).
The email carries a signed confirm link, never a manage token: the old
link dies only when the inbox holder POSTs the confirm form. So someone
who knows a teacher's address cannot lock them out, school mail scanners
that prefetch links (GET) change nothing, and live capabilities never sit
in mail archives.
"""

from __future__ import annotations

import hashlib
import re
import time

from flask import (
    Blueprint, abort, current_app, g, redirect, render_template, render_template_string,
    request, url_for,
)
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import collections_repo as repo
from .. import mail
from . import limiter
from .views_class import MIN_SUBMIT_SECONDS

bp = Blueprint("recover", __name__)

CONFIRM_MAX_AGE = 86400  # confirm links work for 24 hours
SUBJECT = "Your NonNewtonian Physicists class link"
USED_OR_EXPIRED = "This link was already used or has expired."


def _db():
    return current_app.get_db()


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="nnp-reissue")


def _abs(path: str) -> str:
    # Production preflight guarantees an https SITE_URL; the request host
    # fallback is for dev only.
    base = current_app.config["SITE_URL"].rstrip("/") or request.host_url.rstrip("/")
    return base + path


def _rcpt_key() -> str:
    email = request.form.get("email", "").strip().lower()
    return "rcpt:" + hashlib.sha256(email.encode()).hexdigest()


def _one_line_ascii(text: str) -> str:
    """A class name as typed by a stranger: one line, printable ASCII, 80 chars."""
    return re.sub(r"[^\x20-\x7e]", "", re.sub(r"\s+", " ", text)).strip()[:80]


@bp.route("/recover")
def form():
    return render_template("recover.html", smtp=current_app.config["SMTP_CONFIGURED"],
                           form_ts=int(time.time()))


# The per-recipient and global budgets are spent only by requests that
# get past the spam traps (rcpt) or actually send mail (global), so junk
# posts and random addresses cannot shut /recover off for everyone. A
# known address can still be spent 3 times a day, but each of those sends
# that teacher a working confirm link, so it is spam, not a lockout.
# Accepted: someone who registers many classes under addresses they
# control can still spend the global 100/day mail budget; the 429 page
# below then points at the contact fallback.
@bp.route("/recover", methods=["POST"])
@limiter.limit("5 per hour")
@limiter.limit("3 per day", key_func=_rcpt_key,
               deduct_when=lambda resp: g.get("recover_past_traps", False))
@limiter.limit("100 per day", key_func=lambda: "recover-global",
               deduct_when=lambda resp: g.get("recover_mailed", False))
def send_link():
    if not current_app.config["SMTP_CONFIGURED"]:
        abort(404)
    # Spam traps, as on the student submit form.
    if request.form.get("website"):
        abort(400)
    try:
        elapsed = time.time() - int(request.form.get("form_ts", "0"))
    except ValueError:
        elapsed = 0
    if elapsed < MIN_SUBMIT_SECONDS:
        abort(400)
    g.recover_past_traps = True

    email = request.form.get("email", "").strip()
    rows = _db().execute(
        "SELECT id, name, slug, manage_token_hash FROM collections "
        "WHERE lower(teacher_email)=lower(?) LIMIT 10", (email,)).fetchall()
    if rows:
        g.recover_mailed = True
        _mail_links(email, rows)
    # Identical response whether or not the address matched anything.
    return render_template("recover_sent.html")


_TOO_MANY = """{% extends "base.html" %}
{% block title %}Too many requests - {{ site_name }}{% endblock %}
{% block content %}
  <h1>Too many requests</h1>
  <p>Email recovery is busy for now; try again tomorrow. Or email
    <a href="mailto:mglerner@smith.edu">mglerner@smith.edu</a> FROM the address you gave when
    creating the class, with the class name; I will verify and send a new link.</p>
{% endblock %}"""


@bp.errorhandler(429)
def _too_many(exc):
    return render_template_string(_TOO_MANY), 429


def _mail_links(email, rows) -> None:
    ser = _serializer()
    parts = ["Someone (hopefully you) asked for a new management link for the class(es) below.",
             "Each confirm link works for 24 hours. Opening it changes nothing until you press",
             "the button on that page; then your old management link stops working.",
             "If this wasn't you, ignore this email.", ""]
    for row in rows:
        signed = ser.dumps({"c": row["id"], "h": row["manage_token_hash"][:16]})
        parts += [
            "Class name (as typed by whoever created it): " + _one_line_ascii(row["name"]),
            "Student link: " + _abs(url_for("cls.submit", slug=row["slug"])),
            "Confirm a new management link: " + _abs(url_for("recover.confirm", signed=signed)),
            "",
        ]
    try:
        mail.send(current_app.config, email, SUBJECT, "\n".join(parts))
    except Exception as exc:  # never log the address or the link
        current_app.logger.error("recover mail failed: %s", type(exc).__name__)


def _verified_collection(signed):
    """The collection a confirm link names, or None if the signature is bad
    or expired, or the link was already used (token hash has moved on)."""
    try:
        data = _serializer().loads(signed, max_age=CONFIRM_MAX_AGE)
    except BadSignature:  # includes SignatureExpired
        return None
    row = _db().execute("SELECT * FROM collections WHERE id=?", (data["c"],)).fetchone()
    if not row or not row["manage_token_hash"].startswith(data["h"]):
        return None
    return row


@bp.route("/recover/confirm/<signed>")
def confirm(signed):
    # NEVER mutate here: school mail scanners prefetch links.
    coll = _verified_collection(signed)
    if coll is None:
        return render_template("recover_confirm.html", error=USED_OR_EXPIRED), 400
    return render_template("recover_confirm.html", coll=coll, signed=signed, error=None)


@bp.route("/recover/confirm/<signed>", methods=["POST"])
def confirm_post(signed):
    coll = _verified_collection(signed)
    token = None
    if coll is not None:
        token = repo.reissue_manage_token(_db(), coll["id"],
                                          expected_hash=coll["manage_token_hash"])
    if token is None:
        return render_template("recover_confirm.html", error=USED_OR_EXPIRED), 400
    return redirect(url_for("manage.welcome", token=token))
