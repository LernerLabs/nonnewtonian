"""M7 WP1: production mode (wsgi entrypoint, preflight, ProxyFix, cookies),
the https CSRF regression, token redaction in error logs, and the
friendly CSRF page."""

import importlib
import logging
import re
import sys

import pytest
from flask import request
from werkzeug.middleware.proxy_fix import ProxyFix

from nonnewtonian import db as db_mod
from nonnewtonian.web import create_app

NOW = "2026-07-07T00:00:00+00:00"
SECRET = "s" * 40
ADMIN = "a" * 40
NNP_VARS = ("NNP_DB", "NNP_PHOTOS", "NNP_SITE_URL", "NNP_SECRET_KEY", "NNP_ADMIN_TOKEN",
            "NNP_SMTP_HOST", "NNP_SMTP_PORT", "NNP_SMTP_USER", "NNP_SMTP_PASSWORD",
            "NNP_MAIL_FROM")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in NNP_VARS:
        monkeypatch.delenv(var, raising=False)


def _migrated_db(tmp_path, mode=0o600):
    db_path = tmp_path / "app.db"
    db_mod.init_db(db_path, now=NOW).close()
    db_path.chmod(mode)
    return db_path


def _prod_config(tmp_path, **overrides):
    cfg = {"DB_PATH": str(_migrated_db(tmp_path)), "PHOTO_DIR": str(tmp_path / "photos"),
           "SITE_URL": "https://example.org", "SECRET_KEY": SECRET, "ADMIN_TOKEN": ADMIN}
    cfg.update(overrides)
    return cfg


def _preflight_error(cfg) -> str:
    with pytest.raises(RuntimeError) as exc:
        create_app(cfg, production=True)
    return str(exc.value)


# --- preflight --------------------------------------------------------------

def test_valid_production_config_boots(tmp_path):
    app = create_app(_prod_config(tmp_path), production=True)
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert isinstance(app.wsgi_app, ProxyFix)


@pytest.mark.parametrize("override, expected", [
    ({"SECRET_KEY": None}, "NNP_SECRET_KEY missing or shorter than 32 chars"),
    ({"SECRET_KEY": "short"}, "NNP_SECRET_KEY missing or shorter than 32 chars"),
    ({"ADMIN_TOKEN": "dev-admin-token"}, "NNP_ADMIN_TOKEN is the dev default"),
    ({"ADMIN_TOKEN": "short"}, "NNP_ADMIN_TOKEN is the dev default"),
    ({"SITE_URL": "http://example.org"}, "NNP_SITE_URL must start with https://"),
    ({"SITE_URL": ""}, "NNP_SITE_URL must start with https://"),
    ({"PHOTO_DIR": "data/photos"}, "NNP_PHOTOS must be an absolute path"),
    ({"SMTP_HOST": "smtp.example.org"}, "SMTP is half-configured"),
])
def test_each_config_check_fails_individually(tmp_path, monkeypatch, override, expected):
    monkeypatch.chdir(tmp_path)  # a relative PHOTO_DIR is created here, not in the repo
    msg = _preflight_error(_prod_config(tmp_path, **override))
    assert msg.startswith("production preflight failed:")
    assert expected in msg
    assert msg.count("\n  - ") == 1


def test_relative_db_path_fails(tmp_path, monkeypatch):
    _migrated_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    msg = _preflight_error(_prod_config(tmp_path, DB_PATH="app.db"))
    assert "NNP_DB must be an absolute path" in msg
    assert msg.count("\n  - ") == 1


def test_missing_db_fails_without_creating_it(tmp_path):
    missing = tmp_path / "nope.db"
    msg = _preflight_error(_prod_config(tmp_path, DB_PATH=str(missing)))
    assert f"database missing at {missing}" in msg
    assert "nonnewtonian migrate --db" in msg
    assert not missing.exists()  # sqlite3.connect would have created it


def test_group_readable_db_fails(tmp_path):
    cfg = _prod_config(tmp_path)
    (tmp_path / "app.db").chmod(0o640)
    assert "database must be mode 0600" in _preflight_error(cfg)


def test_pending_migrations_fail(tmp_path):
    cfg = _prod_config(tmp_path)
    conn = db_mod.connect(cfg["DB_PATH"])
    conn.execute("DELETE FROM applied_migrations WHERE name = (SELECT max(name) FROM applied_migrations)")
    conn.commit()
    conn.close()
    last = db_mod.migration_files()[-1].name
    assert f"pending migrations {last}" in _preflight_error(cfg)


def test_no_migrations_found_fails(tmp_path, monkeypatch):
    cfg = _prod_config(tmp_path)
    monkeypatch.setattr(db_mod, "migration_files", lambda migrations_dir=None: [])
    assert "no migrations found under" in _preflight_error(cfg)


def test_all_problems_reported_in_one_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    msg = _preflight_error({
        "DB_PATH": "missing.db", "PHOTO_DIR": "photos", "SITE_URL": "http://x",
        "SECRET_KEY": None, "ADMIN_TOKEN": "dev-admin-token", "SMTP_USER": "me"})
    for expected in ("NNP_SECRET_KEY", "NNP_ADMIN_TOKEN", "NNP_SITE_URL", "NNP_DB must",
                     "NNP_PHOTOS must", "SMTP is half-configured", "database missing"):
        assert expected in msg
    assert msg.count("\n  - ") == 7


def test_non_production_app_boots_with_no_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()
    assert app.config["SECRET_KEY"]
    assert not isinstance(app.wsgi_app, ProxyFix)
    assert app.config["SMTP_CONFIGURED"] is False


def test_wsgi_module_imports(tmp_path, monkeypatch):
    db_path = _migrated_db(tmp_path)
    monkeypatch.setenv("NNP_DB", str(db_path))
    monkeypatch.setenv("NNP_PHOTOS", str(tmp_path / "photos"))
    monkeypatch.setenv("NNP_SITE_URL", "https://example.org")
    monkeypatch.setenv("NNP_SECRET_KEY", SECRET)
    monkeypatch.setenv("NNP_ADMIN_TOKEN", ADMIN)
    monkeypatch.delitem(sys.modules, "nonnewtonian.web.wsgi", raising=False)
    wsgi = importlib.import_module("nonnewtonian.web.wsgi")
    monkeypatch.delitem(sys.modules, "nonnewtonian.web.wsgi")
    assert isinstance(wsgi.app.wsgi_app, ProxyFix)
    assert wsgi.app.config["DB_PATH"] == str(db_path)


def test_wsgi_module_refuses_dev_admin_token(tmp_path, monkeypatch):
    monkeypatch.setenv("NNP_DB", str(_migrated_db(tmp_path)))
    monkeypatch.setenv("NNP_PHOTOS", str(tmp_path / "photos"))
    monkeypatch.setenv("NNP_ADMIN_TOKEN", "dev-admin-token")
    monkeypatch.delitem(sys.modules, "nonnewtonian.web.wsgi", raising=False)
    with pytest.raises(RuntimeError) as exc:
        importlib.import_module("nonnewtonian.web.wsgi")
    monkeypatch.delitem(sys.modules, "nonnewtonian.web.wsgi", raising=False)
    msg = str(exc.value)
    for expected in ("NNP_SECRET_KEY", "NNP_ADMIN_TOKEN", "NNP_SITE_URL"):
        assert expected in msg


# --- ProxyFix ---------------------------------------------------------------

def _ip_app(app):
    app.add_url_rule("/_ip", "ip", lambda: request.remote_addr)
    return app.test_client()


def test_production_trusts_last_xff_hop(tmp_path):
    c = _ip_app(create_app(_prod_config(tmp_path), production=True))
    r = c.get("/_ip", environ_base={"REMOTE_ADDR": "127.0.0.1"},
              headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert r.data == b"203.0.113.9"


def test_dev_ignores_xff(tmp_path):
    c = _ip_app(create_app({"DB_PATH": str(_migrated_db(tmp_path)),
                            "PHOTO_DIR": str(tmp_path / "photos")}))
    r = c.get("/_ip", environ_base={"REMOTE_ADDR": "127.0.0.1"},
              headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert r.data == b"127.0.0.1"


# --- CSRF over https --------------------------------------------------------

def _csrf_app(tmp_path):
    # CSRF ENABLED (TESTING alone does not disable Flask-WTF's check).
    return create_app({"DB_PATH": str(_migrated_db(tmp_path)),
                       "PHOTO_DIR": str(tmp_path / "photos"), "TESTING": True})


def test_https_post_with_valid_csrf_token_is_accepted(tmp_path):
    # Regression (reproduced 2026-10-20 before the fix): WTF_CSRF_SSL_STRICT
    # plus Referrer-Policy: no-referrer made EVERY https POST return 400
    # "The referrer header is missing." Plain-http tests never saw it.
    c = _csrf_app(tmp_path).test_client()
    html = c.get("/new", base_url="https://localhost").data.decode()
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)
    r = c.post("/new", base_url="https://localhost", data={
        "csrf_token": token, "name": "Chem", "textbook_mode": "chapters",
        "custom_title": "My Chem Book", "chapter_lines": "Atoms\nBonds"})
    assert r.status_code == 302, r.data[:300]
    assert "/manage/" in r.headers["Location"]


def test_csrf_error_page(tmp_path):
    c = _csrf_app(tmp_path).test_client()
    r = c.post("/new", data={"name": "Chem"})
    assert r.status_code == 400
    assert b"Press Back" in r.data


# --- logging and headers ----------------------------------------------------

def test_manage_exception_logged_redacted(tmp_path, caplog):
    app = create_app({"DB_PATH": str(_migrated_db(tmp_path)),
                      "PHOTO_DIR": str(tmp_path / "photos"), "PROPAGATE_EXCEPTIONS": False})

    def boom(token):
        raise ValueError("kaboom")

    app.view_functions["manage.dashboard"] = boom
    secret_token = "tok-SECRET-1234567890"
    with caplog.at_level(logging.ERROR):
        r = app.test_client().get(f"/manage/{secret_token}")
    assert r.status_code == 500
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Exception on /manage/REDACTED [GET]" in logged
    assert secret_token not in logged
    assert secret_token not in caplog.text


def test_recover_confirm_exception_logged_redacted(tmp_path, caplog):
    # /recover/confirm/<signed>: the whole remainder of the path must go.
    app = create_app({"DB_PATH": str(_migrated_db(tmp_path)),
                      "PHOTO_DIR": str(tmp_path / "photos"), "PROPAGATE_EXCEPTIONS": False})

    def boom(signed):
        raise ValueError("kaboom")

    app.view_functions["recover.confirm"] = boom
    signed = "SIGNED-SECRET-abcdef"
    with caplog.at_level(logging.ERROR):
        app.test_client().get(f"/recover/confirm/{signed}")
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Exception on /recover/REDACTED [GET]" in logged
    assert signed not in caplog.text


@pytest.mark.parametrize("path", ["/manage/x", "/admin/x"])
def test_token_paths_are_no_store(tmp_path, path):
    app = create_app({"DB_PATH": str(_migrated_db(tmp_path)),
                      "PHOTO_DIR": str(tmp_path / "photos")})
    r = app.test_client().get(path)
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Robots-Tag"] == "noindex"


def test_public_paths_are_cacheable(tmp_path):
    app = create_app({"DB_PATH": str(_migrated_db(tmp_path)),
                      "PHOTO_DIR": str(tmp_path / "photos")})
    assert "no-store" not in app.test_client().get("/about").headers.get("Cache-Control", "")
