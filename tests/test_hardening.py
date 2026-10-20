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
    # succeeds.  The slot stays held while the deck is being sent (M7
    # review #3) and is freed by the WSGI close(), which gunicorn always
    # calls and the test client calls on r.close().
    r = client.get(url)
    assert r.status_code == 200 and r.data[:2] == b"PK"
    assert not views_public._HEAVY_BUILD.acquire(blocking=False)
    r.close()
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


def test_deck_wait_is_short():
    # M7 review #17: each waiter holds one of gunicorn's 8 threads.
    assert views_public.HEAVY_BUILD_WAIT_SECONDS <= 2


def test_deck_routes_rate_limited(class_app):
    """M7 review #17: 6 per minute per IP on both deck routes."""
    for route in ("textbook_deck", "deck"):
        fn = (views_public if route == "textbook_deck" else views_class).__dict__[route]
        src = inspect.getsource(fn)
        assert '@limiter.limit(DECK_RATE_LIMIT)' in src, route
    assert views_public.DECK_RATE_LIMIT == "6 per minute"
    client = class_app.test_client()
    codes = []
    for _ in range(7):
        r = client.get(f"/c/{class_app._slug}/deck.pptx")
        codes.append(r.status_code)
        r.close()
    assert codes == [200] * 6 + [429]


def test_oversized_deck_refused_before_building(seeded_client, monkeypatch, tmp_path):
    """M7 review #3: a deck whose stored photos exceed the cap gets a clear
    413 without building (or taking the slot)."""
    photo = tmp_path / "big.jpg"
    photo.write_bytes(b"\xff" * 4096)
    monkeypatch.setattr(views_public, "_entry_image_paths", lambda entry: [photo])
    monkeypatch.setattr(views_public, "DECK_PHOTO_BYTES_CAP", 1024)

    def must_not_build(*a, **k):
        raise AssertionError("deck was built")

    monkeypatch.setattr(views_public, "build_deck", must_not_build)
    r = seeded_client.get("/textbooks/knight-calc-3rd/deck.pptx")
    assert r.status_code == 413
    assert b"one entry at a time" in r.data
    assert views_public._HEAVY_BUILD.acquire(blocking=False)
    views_public._HEAVY_BUILD.release()


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


def test_unreachable_photo_url_is_blamed_on_the_link(class_app, monkeypatch):
    """M7 review #16: a dead link must not read as our storage failure."""
    import requests

    class DeadSession:
        def get(self, url, **kwargs):
            raise requests.exceptions.ConnectTimeout("host down")

    real_fetch = views_class.fetch_photo
    monkeypatch.setattr(views_class, "fetch_photo",
                        lambda url, d: real_fetch(url, d, session=DeadSession(),
                                                  resolver=_resolver))
    r = class_app.test_client().post(
        f"/c/{class_app._slug}/submit",
        data={"scientist_name": "Lise Meitner", "chapter": "3",
              "description": "Explained nuclear fission.", "license_grant": "1",
              "form_ts": "0", "photo_url": "https://pub.example/m.jpg"})
    assert r.status_code == 200 and b"Thanks" in r.data
    assert b"Could not download that link" in r.data
    assert b"could not store the file" not in r.data


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
    # Injected-session path only: each 1-byte chunk "takes" one second.
    # This fake ignores chunk_size, so it cannot show the real blocking
    # behaviour; test_fetch_deadline_real_stack below does (M7 review #1).
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(photos.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(photos.PhotoError, match="too slow"):
        photos.fetch_photo("https://pub.example/a.jpg", tmp_path,
                           session=_TrickleSession(), resolver=_resolver)
    assert not list(tmp_path.rglob("*.*"))


def _trickle_server(monkeypatch, *, head_gap, body_gap, give_up_after=6.0):
    """Route the real requests/urllib3 stack (with pinning) to one end of
    a socketpair -- the sandbox forbids listening sockets -- and have a
    thread on the other end send a 200 head (1 byte per `head_gap` s, or
    all at once if 0) and then a 1 MB body (1 byte per `body_gap` s).
    Each recv gets a byte well inside FETCH_TIMEOUT, so only a total
    deadline can stop it.  The server gives up after `give_up_after` s so
    the old code fails rather than hangs."""
    import socket
    import threading
    import urllib3.util.connection as u3conn

    head = b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n"
    dialed = []
    ours, theirs = socket.socketpair()

    def fake_create_connection(address, *a, **k):
        dialed.append(address)
        return ours

    monkeypatch.setattr(u3conn, "create_connection", fake_create_connection)
    # requests honours *_PROXY from the environment (sandboxes set them);
    # through a proxy neither the pin nor this fake would be in the path.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)

    def serve():
        stop = time.monotonic() + give_up_after
        try:
            theirs.recv(65536)  # the request
            if head_gap:
                for byte in head:
                    if time.monotonic() > stop:
                        return
                    theirs.sendall(bytes([byte]))
                    time.sleep(head_gap)
            else:
                theirs.sendall(head)
            while time.monotonic() < stop:
                theirs.sendall(b"\xff")
                time.sleep(body_gap)
        except OSError:  # our end was shut down: the fix worked
            return
        finally:
            theirs.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return dialed, thread


@pytest.mark.parametrize("head_gap", [0, 0.05], ids=["body-trickle", "header-trickle"])
def test_fetch_deadline_real_stack(tmp_path, monkeypatch, head_gap):
    """A server trickling 1 byte per 0.05 s, in the body (urllib3 2.x
    blocks in stream() until it has a whole 64 KiB chunk) or in the
    status line/headers (read before any chunk loop), must be cut off at
    FETCH_DEADLINE.  Before the M7 review #1 fix this ran until the
    server gave up and then failed as a broken download, not "too slow"."""
    monkeypatch.setattr(photos, "FETCH_DEADLINE", 0.5)
    dialed, thread = _trickle_server(monkeypatch, head_gap=head_gap, body_gap=0.05)
    t0 = time.monotonic()
    with pytest.raises(photos.PhotoError, match="too slow"):
        photos.fetch_photo("http://pub.example/a.jpg", tmp_path, resolver=_resolver)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5 + 1.5, elapsed
    assert dialed == [("93.184.216.34", 80)]  # pinned to the vetted IP
    thread.join(timeout=10)
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
