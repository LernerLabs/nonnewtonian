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
import re
import secrets
import types
from pathlib import Path

from flask import Flask, g, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import db as db_mod

csrf = CSRFProtect()
# In-memory rate-limit store: correct only because gunicorn runs ONE
# process (deploy/gunicorn.conf.py: 1 worker x 8 threads on a 512MB box).
# Counters are per-process and reset on restart; accepted at this scale.
limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri="memory://")


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def create_app(config: dict | None = None, *, production: bool = False) -> Flask:
    app = Flask(__name__)
    secret = os.environ.get("NNP_SECRET_KEY")
    app.config.update(
        DB_PATH=os.environ.get("NNP_DB", "data/app.db"),
        PHOTO_DIR=os.environ.get("NNP_PHOTOS", "data/photos"),
        ADMIN_TOKEN=os.environ.get("NNP_ADMIN_TOKEN", "dev-admin-token"),
        # Dev/test: a per-boot random key is fine. Production: NNP_SECRET_KEY
        # is required (preflight), because a key that changes on restart
        # invalidates every open form's CSRF token.
        SECRET_KEY=secret or (None if production else secrets.token_hex(32)),
        SITE_NAME="NonNewtonian Physicists",
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # cap upload bodies
        SITE_URL=os.environ.get("NNP_SITE_URL", ""),
        # Flask-WTF default 3600s would 400 a student who spends >1h on the
        # form; the token stays bound to the session cookie.
        WTF_CSRF_TIME_LIMIT=None,
        # VERIFIED 2026-10-20: with the strict Referer check on, every https
        # POST returns 400 because Referrer-Policy is no-referrer. The
        # synchronizer token + Secure/SameSite=Lax cookie are the defense.
        WTF_CSRF_SSL_STRICT=False,
        SMTP_HOST=os.environ.get("NNP_SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("NNP_SMTP_PORT", "587")),
        SMTP_USER=os.environ.get("NNP_SMTP_USER", ""),
        SMTP_PASSWORD=os.environ.get("NNP_SMTP_PASSWORD", ""),
        MAIL_FROM=os.environ.get("NNP_MAIL_FROM", ""),
    )
    if config:
        app.config.update(config)
    app.config["SMTP_CONFIGURED"] = all(
        app.config[k] for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"))
    if production:
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax",
                          PREFERRED_URL_SCHEME="https")
        # x_for=1 is safe ONLY because gunicorn binds 127.0.0.1 and the sole
        # peer is Caddy, which appends the real client as the last XFF hop.
        # Not enabled in dev, where a client could spoof X-Forwarded-For.
        # This is also what makes the limiter key on the real client IP.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    # Startup preflight: fail loudly at boot, not at request time.
    _preflight(app, production=production)

    csrf.init_app(app)
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
        if request.path.startswith(("/manage/", "/admin/", "/recover/")):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Robots-Tag"] = "noindex"
        return resp

    # Capability tokens live in URL paths; keep them out of the error log.
    _token_path = re.compile(r"^/(manage|admin|recover)/.+")

    def _log_exception(self, exc_info):
        path = _token_path.sub(r"/\1/REDACTED", request.path)
        self.logger.error("Exception on %s [%s]", path, request.method, exc_info=exc_info)

    app.log_exception = types.MethodType(_log_exception, app)

    @app.errorhandler(CSRFError)
    def _csrf_error(exc):
        return render_template("csrf_error.html"), 400

    app.get_db = get_db  # type: ignore[attr-defined]
    app.utcnow = _utcnow  # type: ignore[attr-defined]

    from . import views_public, views_admin, views_manage, views_class, views_recover
    app.register_blueprint(views_public.bp)
    app.register_blueprint(views_admin.bp)
    app.register_blueprint(views_manage.bp)
    app.register_blueprint(views_class.bp)
    app.register_blueprint(views_recover.bp)

    return app


def _preflight(app: Flask, *, production: bool = False) -> None:
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
    if production:
        problems = _production_checks(app)
        if problems:
            raise RuntimeError("production preflight failed:\n  - " + "\n  - ".join(problems))


def _production_checks(app: Flask) -> list[str]:
    """Every production misconfiguration at once, one string per problem,
    so a first boot shows the whole list instead of one error per restart."""
    cfg = app.config
    problems: list[str] = []
    if not cfg.get("SECRET_KEY") or len(cfg["SECRET_KEY"]) < 32:
        problems.append("NNP_SECRET_KEY missing or shorter than 32 chars")
    if cfg["ADMIN_TOKEN"] == "dev-admin-token" or len(cfg["ADMIN_TOKEN"]) < 32:
        problems.append("NNP_ADMIN_TOKEN is the dev default or shorter than 32 chars")
    if not cfg["SITE_URL"].startswith("https://"):
        problems.append("NNP_SITE_URL must start with https:// "
                        "(kit/manage links and the embed origin guard depend on it)")
    for key, var in (("DB_PATH", "NNP_DB"), ("PHOTO_DIR", "NNP_PHOTOS")):
        if not Path(cfg[key]).is_absolute():
            problems.append(f"{var} must be an absolute path (got {cfg[key]!r})")
    smtp = [cfg[k] for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM")]
    if any(smtp) and not all(smtp):
        problems.append("SMTP is half-configured; set all of NNP_SMTP_HOST, NNP_SMTP_USER, "
                        "NNP_SMTP_PASSWORD, NNP_MAIL_FROM or none")
    if not db_mod.migration_files():
        problems.append(f"no migrations found under {db_mod._MIGRATIONS_DIR}; "
                        "the server install must be editable (pip install -e)")

    db_path = Path(cfg["DB_PATH"])
    # Checked BEFORE connecting: sqlite3.connect silently creates an empty file.
    if not db_path.exists():
        problems.append(f"database missing at {db_path}; seed it on the laptop and upload, "
                        f"or run: nonnewtonian migrate --db {db_path}")
        return problems
    if db_path.stat().st_mode & 0o077:
        problems.append("database must be mode 0600 (caddy is in group nnp)")
    conn = db_mod.connect(db_path)
    try:
        pending = db_mod.pending_migrations(conn)
    finally:
        conn.close()
    if pending:
        problems.append(f"pending migrations {', '.join(pending)}; "
                        f"run: nonnewtonian migrate --db {db_path}")
    return problems
