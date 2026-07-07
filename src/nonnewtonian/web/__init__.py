"""Flask application factory for NonNewtonian Physicists.

M3 scope: the read-only communal site (browse-first — every scientist's
photo, writeup, sources, and placements are readable in the browser with
no download) plus a token-gated admin queue to approve seeded entries
onto the public pages.  Class collections, submissions, and moderation
arrive in M4+.
"""

from __future__ import annotations

import datetime as _dt
import os
import secrets
from pathlib import Path

from flask import Flask, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

from .. import db as db_mod

csrf = CSRFProtect()
# In-memory rate-limit store: intentional for a single small VPS (one
# process). If M7 ever runs multiple workers, switch storage_uri to a
# shared backend so limits are counted across them.
limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri="memory://")


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DB_PATH=os.environ.get("NNP_DB", "data/app.db"),
        PHOTO_DIR=os.environ.get("NNP_PHOTOS", "data/photos"),
        ADMIN_TOKEN=os.environ.get("NNP_ADMIN_TOKEN", "dev-admin-token"),
        SECRET_KEY=os.environ.get("NNP_SECRET_KEY", secrets.token_hex(32)),
        SITE_NAME="NonNewtonian Physicists",
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # cap upload bodies
        SITE_URL=os.environ.get("NNP_SITE_URL", ""),
        SMTP_CONFIGURED=False,
    )
    if config:
        app.config.update(config)

    # Startup preflight: fail loudly at boot, not at request time.
    _preflight(app)

    csrf.init_app(app)
    # Rate-limit storage: in-memory by default (fine for one small VPS);
    # note for M7: put Werkzeug ProxyFix in front so the limiter keys on
    # the real client IP, not Caddy's loopback address.
    limiter.init_app(app)

    def get_db():
        if "db" not in g:
            g.db = db_mod.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close_db(exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.after_request
    def _security_headers(resp):
        # Clickjacking: deny framing everywhere the view didn't explicitly
        # opt in with a CSP frame-ancestors (only /c/<slug>/embed does), so
        # the token-bearing manage/moderation pages can't be framed to
        # trick a teacher into approving/deleting (M4 review).
        csp = resp.headers.get("Content-Security-Policy", "")
        if "frame-ancestors" not in csp:
            resp.headers.setdefault("X-Frame-Options", "DENY")
        # The manage token lives in the URL path; no-referrer keeps it out
        # of the Referer header on any outbound link (mailto, Wikipedia).
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    app.get_db = get_db  # type: ignore[attr-defined]
    app.utcnow = _utcnow  # type: ignore[attr-defined]

    from . import views_public, views_admin, views_manage, views_class
    app.register_blueprint(views_public.bp)
    app.register_blueprint(views_admin.bp)
    app.register_blueprint(views_manage.bp)
    app.register_blueprint(views_class.bp)

    return app


def _preflight(app: Flask) -> None:
    """Refuse to start on a broken environment (the plan's hard-fail)."""
    try:
        import PIL  # noqa: F401
        import pptx  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"slide dependency missing: {exc}") from exc

    db_path = Path(app.config["DB_PATH"])
    if db_path.exists():
        conn = db_mod.connect(db_path)
        try:
            db_mod.assert_wal(conn)
        finally:
            conn.close()
    photo_dir = Path(app.config["PHOTO_DIR"])
    photo_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(photo_dir, os.W_OK):  # pragma: no cover
        raise RuntimeError(f"photo dir not writable: {photo_dir}")
