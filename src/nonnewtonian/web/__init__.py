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
from pathlib import Path

from flask import Flask, g

from .. import db as db_mod


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DB_PATH=os.environ.get("NNP_DB", "data/app.db"),
        PHOTO_DIR=os.environ.get("NNP_PHOTOS", "data/photos"),
        ADMIN_TOKEN=os.environ.get("NNP_ADMIN_TOKEN", "dev-admin-token"),
        SITE_NAME="NonNewtonian Physicists",
    )
    if config:
        app.config.update(config)

    # Startup preflight: fail loudly at boot, not at request time.
    _preflight(app)

    def get_db():
        if "db" not in g:
            g.db = db_mod.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close_db(exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    app.get_db = get_db  # type: ignore[attr-defined]
    app.utcnow = _utcnow  # type: ignore[attr-defined]

    from . import views_public, views_admin
    app.register_blueprint(views_public.bp)
    app.register_blueprint(views_admin.bp)

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
