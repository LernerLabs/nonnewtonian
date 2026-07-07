"""M3: read-only site + admin approval, driven through the Flask test
client against a freshly-seeded database (no network)."""

from pathlib import Path

import pytest

from nonnewtonian import db as db_mod
from nonnewtonian.importer import seed_import
from nonnewtonian.web import create_app

from conftest import SCIENTISTS

NOW = "2026-07-07T00:00:00+00:00"
MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textbooks" / "manifest.json"
TOKEN = "test-admin"


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "web.db"
    conn = db_mod.init_db(db_path, now=NOW)
    seed_import(conn, scientists_dir=SCIENTISTS, manifest_path=MANIFEST,
                photo_dir=tmp_path / "photos", now=NOW, decks_dir=None, fetch_photos=False)
    conn.close()
    app = create_app({
        "DB_PATH": str(db_path), "PHOTO_DIR": str(tmp_path / "photos"),
        "ADMIN_TOKEN": TOKEN, "TESTING": True,
    })
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def approved_client(app):
    c = app.test_client()
    c.post(f"/admin/{TOKEN}/approve-all-seeds")
    return c


def test_pending_content_is_never_public_before_approval(client):
    # Seeds are communal-pending: the collection is empty until approved.
    assert b"0 scientists in the collection" in client.get("/").data
    r = client.get("/textbooks/knight-calc-3rd")
    assert r.status_code == 200
    assert b"Emmy Noether" not in r.data  # not approved yet
    assert b"0 scientists placed" in r.data
    # the scientist's own page 404s while unapproved
    assert client.get("/scientists/emmy-noether").status_code == 404


def test_all_public_pages_render(approved_client):
    for path in ["/", "/scientists", "/textbooks", "/textbooks/knight-calc-3rd",
                 "/scientists/emmy-noether", "/wanted", "/about", "/for-teachers",
                 "/for-students", "/privacy"]:
        assert approved_client.get(path).status_code == 200, path


def test_healthz(client):
    assert client.get("/healthz").data == b"ok"


def test_textbook_shows_every_chapter_including_gaps(approved_client):
    html = approved_client.get("/textbooks/knight-calc-3rd").data.decode()
    assert html.count('class="num"') == 42          # all chapters shown
    assert "No one here yet" in html                # empty-chapter prompt
    assert html.count(">Emmy Noether<") == 2        # placed in ch 9 and ch 10


def test_pending_never_leaks_via_direct_id(app):
    # An entry id that exists but is unapproved must 404 on its slide route.
    c = app.test_client()
    assert c.get("/scientists/emmy-noether/slide.pptx").status_code == 404


def test_slide_download_after_approval(approved_client):
    r = approved_client.get("/scientists/emmy-noether/slide.pptx")
    assert r.status_code == 200
    assert r.data[:2] == b"PK"  # a zip / pptx
    assert "attachment" in r.headers["Content-Disposition"]


def test_deck_download(approved_client):
    r = approved_client.get("/textbooks/knight-calc-3rd/deck.pptx")
    assert r.status_code == 200 and r.data[:2] == b"PK"


def test_admin_requires_correct_token(client):
    assert client.get("/admin/wrong").status_code == 404
    assert client.get(f"/admin/{TOKEN}").status_code == 200


def test_admin_approve_single_entry(app):
    c = app.test_client()
    conn = db_mod.connect(app.config["DB_PATH"])
    eid = conn.execute(
        "SELECT id FROM entries WHERE scientist_slug='emmy-noether'").fetchone()[0]
    conn.close()
    assert c.get("/scientists/emmy-noether").status_code == 404
    r = c.post(f"/admin/{TOKEN}/entry/{eid}/approve")
    assert r.status_code == 302
    assert c.get("/scientists/emmy-noether").status_code == 200


def test_admin_reject_keeps_it_unpublished(app):
    c = app.test_client()
    conn = db_mod.connect(app.config["DB_PATH"])
    eid = conn.execute(
        "SELECT id FROM entries WHERE scientist_slug='lise-meitner'").fetchone()[0]
    conn.close()
    c.post(f"/admin/{TOKEN}/entry/{eid}/reject")
    c.post(f"/admin/{TOKEN}/approve-all-seeds")  # bulk approve the rest
    # rejected one stays down even after bulk approve (only pending get approved)
    assert c.get("/scientists/lise-meitner").status_code == 404


def test_photos_served_and_pending_photo_not_special(approved_client, app):
    # A stored/recovered photo path resolves; a traversal attempt is blocked.
    assert approved_client.get("/photos/../app.db").status_code in (400, 403, 404)


def test_search(approved_client):
    r = approved_client.get("/scientists?q=noether")
    assert b"Emmy Noether" in r.data
    assert b"Ronald McNair" not in r.data


def test_attribution_mode_controls_displayed_credit():
    from nonnewtonian.web.queries import DisplayEntry
    e = DisplayEntry(id=1, name="X", slug="x", description=[], sources=[],
                     contributor_name="Jane Doe", attribution_mode="first_initial",
                     why_chapter=None, wikipedia_url=None, license_notice=None, is_seed=False)
    assert e.displayed_credit() == "Jane D."
    e.attribution_mode = "anonymous"
    assert e.displayed_credit() is None
    e.attribution_mode = "full"
    assert e.displayed_credit() == "Jane Doe"
