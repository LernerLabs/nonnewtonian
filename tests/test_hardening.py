"""M7 WP3: request-path hardening -- healthz disk/writable checks, the
one-slot deck-build semaphore, the submit rate-limit string, photo
OSError handling, the total photo-fetch deadline, and atomic photo
writes."""

import inspect
import io
import re
import time
from pathlib import Path

import pytest
from PIL import Image

from nonnewtonian import db as db_mod
from nonnewtonian import photos
from nonnewtonian.importer import seed_import
from nonnewtonian.web import create_app
from nonnewtonian.web import views_class, views_public

from conftest import SCIENTISTS

NOW = "2026-07-07T00:00:00+00:00"
TEXTBOOKS = Path(__file__).resolve().parents[1] / "data" / "textbooks"
TOKEN = "test-admin"


def _jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), (200, 30, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def class_app(tmp_path):
    """A class with a textbook, as in test_collections."""
    db_path = tmp_path / "c.db"
    conn = db_mod.init_db(db_path, now=NOW)
    conn.execute("INSERT INTO textbooks(slug,title,edition,is_builtin,created_at) "
                 "VALUES('bk','The Book','1st',1,?)", (NOW,))
    tid = conn.execute("SELECT id FROM textbooks WHERE slug='bk'").fetchone()[0]
    for ch in range(1, 6):
        conn.execute("INSERT INTO toc_rows(textbook_id,sort_order,chapter,topics) "
                     "VALUES(?,?,?,?)", (tid, ch, ch, f"Topic {ch}"))
    conn.commit()
    conn.close()
    app = create_app({"DB_PATH": str(db_path), "PHOTO_DIR": str(tmp_path / "photos"),
                      "TESTING": True, "WTF_CSRF_ENABLED": False})
    app._db_path = str(db_path)
    client = app.test_client()
    r = client.post("/new", data={"name": "Physics 121", "textbook_mode": "builtin",
                                  "textbook_slug": "bk"})
    assert r.status_code == 302
    app._slug = db_mod.connect(app._db_path).execute(
        "SELECT slug FROM collections").fetchone()[0]
    return app


@pytest.fixture
def seeded_client(tmp_path):
    """Seeded + approved communal site, as in test_web."""
    db_path = tmp_path / "web.db"
    conn = db_mod.init_db(db_path, now=NOW)
    seed_import(conn, scientists_dir=SCIENTISTS, textbooks_dir=TEXTBOOKS,
                photo_dir=tmp_path / "photos", now=NOW, decks_dir=None, fetch_photos=False)
    conn.close()
    app = create_app({"DB_PATH": str(db_path), "PHOTO_DIR": str(tmp_path / "photos"),
                      "ADMIN_TOKEN": TOKEN, "TESTING": True, "WTF_CSRF_ENABLED": False})
    c = app.test_client()
    c.post(f"/admin/{TOKEN}/approve-all-seeds")
    return c


# --- healthz ----------------------------------------------------------------

def test_healthz_ok(class_app):
    r = class_app.test_client().get("/healthz")
    assert r.status_code == 200 and r.data == b"ok"


def test_healthz_503_low_disk(class_app, monkeypatch):
    real = views_public.shutil.disk_usage

    def tiny(path):
        u = real(path)
        return type(u)(u.total, u.total - 1024, 1024)

    monkeypatch.setattr(views_public.shutil, "disk_usage", tiny)
    r = class_app.test_client().get("/healthz")
    assert r.status_code == 503 and r.data == b"low disk"


def test_healthz_503_unwritable(class_app, monkeypatch):
    monkeypatch.setattr(views_public.os, "access", lambda path, mode: False)
    r = class_app.test_client().get("/healthz")
    assert r.status_code == 503 and r.data == b"photo dir not writable"


# --- deck semaphore -----------------------------------------------------------

def _assert_busy_then_free(client, url, monkeypatch):
    monkeypatch.setattr(views_public, "HEAVY_BUILD_WAIT_SECONDS", 0.1)
    assert views_public._HEAVY_BUILD.acquire(blocking=False)
    try:
        t0 = time.monotonic()
        r = client.get(url)
        elapsed = time.monotonic() - t0
    finally:
        views_public._HEAVY_BUILD.release()
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "30"
    assert b"busy" in r.data
    assert elapsed < 0.2 + 0.5  # 0.1s wait; slack for a slow CI box
    # The 503 path must not leak (or over-release) the slot: a build now
    # succeeds, and afterwards the slot is free again.
    r = client.get(url)
    assert r.status_code == 200 and r.data[:2] == b"PK"
    assert views_public._HEAVY_BUILD.acquire(blocking=False)
    views_public._HEAVY_BUILD.release()


def test_deck_busy_503_retry_after_textbook(seeded_client, monkeypatch):
    _assert_busy_then_free(seeded_client, "/textbooks/knight-calc-3rd/deck.pptx", monkeypatch)


def test_deck_busy_503_retry_after_class(class_app, monkeypatch):
    _assert_busy_then_free(class_app.test_client(), f"/c/{class_app._slug}/deck.pptx",
                           monkeypatch)


def test_deck_build_error_releases_slot(class_app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("deck build failed")

    monkeypatch.setattr(views_class, "build_deck", boom)
    class_app.config["PROPAGATE_EXCEPTIONS"] = False
    r = class_app.test_client().get(f"/c/{class_app._slug}/deck.pptx")
    assert r.status_code == 500
    assert views_public._HEAVY_BUILD.acquire(blocking=False)
    views_public._HEAVY_BUILD.release()


def test_class_and_textbook_decks_share_one_semaphore():
    assert views_class.heavy_build is views_public.heavy_build


# --- submit rate limit --------------------------------------------------------

def test_submit_limit_string():
    src = inspect.getsource(views_class)
    m = re.search(r'@limiter\.limit\("([^"]+)"\)\s*\ndef submit_post\b', src)
    assert m, "submit_post rate-limit decorator not found"
    assert m.group(1) == "120 per hour;30 per minute"


# --- photo OSError after the entry committed ------------------------------------

def test_photo_oserror_after_submit_is_warning(class_app, monkeypatch):
    def enospc(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(views_class, "store_bytes", enospc)
    r = class_app.test_client().post(
        f"/c/{class_app._slug}/submit",
        data={"scientist_name": "Chien-Shiung Wu", "chapter": "3",
              "description": "Overturned parity conservation.", "license_grant": "1",
              "form_ts": "0", "photo_file": (io.BytesIO(_jpeg_bytes()), "wu.jpg")},
        content_type="multipart/form-data")
    assert r.status_code == 200 and b"Thanks" in r.data
    assert b"could not store the file" in r.data
    conn = db_mod.connect(class_app._db_path)
    assert conn.execute("SELECT count(*) FROM entries WHERE scientist_name='Chien-Shiung Wu'"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 0


# --- photo fetch total deadline ---------------------------------------------------

class _TrickleResponse:
    status_code = 200
    headers = {}
    is_redirect = False
    is_permanent_redirect = False

    def iter_content(self, chunk_size=65536):
        while True:
            yield b"\xff"


class _TrickleSession:
    def get(self, url, **kwargs):
        return _TrickleResponse()


def _resolver(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", port or 80))]


def test_fetch_deadline(tmp_path, monkeypatch):
    # Each 1-byte chunk "takes" one second: every per-read timeout is
    # satisfied, but the total must stop at FETCH_DEADLINE.
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(photos.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(photos.PhotoError, match="too slow"):
        photos.fetch_photo("https://pub.example/a.jpg", tmp_path,
                           session=_TrickleSession(), resolver=_resolver)
    assert not list(tmp_path.rglob("*.*"))


# --- atomic store_bytes -------------------------------------------------------------

def test_store_bytes_atomic(tmp_path, monkeypatch):
    def fail_replace(src, dst):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(photos.os, "replace", fail_replace)
        with pytest.raises(OSError):
            photos.store_bytes(_jpeg_bytes(), tmp_path)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []

    stored = photos.store_bytes(_jpeg_bytes(), tmp_path)
    assert stored.path.exists()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [stored.path]
