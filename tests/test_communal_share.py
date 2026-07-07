"""M5: a class opts an entry into the communal site; the admin reviews
it; on approval it's cloned into an independent communal entry that
survives deletion of the class."""

from pathlib import Path

import pytest

from nonnewtonian import db as db_mod
from nonnewtonian.web import create_app

NOW = "2026-07-07T00:00:00+00:00"
TOKEN = "admin-tok"


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "m5.db"
    conn = db_mod.init_db(db_path, now=NOW)
    conn.execute("INSERT INTO textbooks(slug,title,edition,is_builtin,created_at) "
                 "VALUES('bk','The Book','1st',1,?)", (NOW,))
    tid = conn.execute("SELECT id FROM textbooks WHERE slug='bk'").fetchone()[0]
    for ch in range(1, 6):
        conn.execute("INSERT INTO toc_rows(textbook_id,sort_order,chapter,topics) "
                     "VALUES(?,?,?,?)", (tid, ch, ch, f"Topic {ch}"))
    conn.commit(); conn.close()
    app = create_app({"DB_PATH": str(db_path), "PHOTO_DIR": str(tmp_path / "photos"),
                      "ADMIN_TOKEN": TOKEN, "TESTING": True, "WTF_CSRF_ENABLED": False})
    app._db_path = str(db_path)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _conn(app):
    return db_mod.connect(app._db_path)


def _class_with_shared_pending_entry(client, app):
    """Create a class, submit + teacher-approve-with-share one entry, so a
    communal-pending shared entry exists. Returns (manage_token, slug, entry_id)."""
    loc = client.post("/new", data={"name": "P121", "textbook_mode": "builtin",
                                     "textbook_slug": "bk", "share_default": "1"}).headers["Location"]
    tok = loc.split("/manage/")[1].split("/")[0]
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    client.post(f"/c/{slug}/submit", data={"scientist_name": "Emmy Noether", "chapter": "3",
                "description": "Symmetry and conservation.", "license_grant": "1", "form_ts": "0"})
    eid = _conn(app).execute("SELECT id FROM entries").fetchone()[0]
    client.post(f"/manage/{tok}/entry/{eid}/approve", data={"share_communal": "1"})
    return tok, slug, eid


def test_shared_entry_shows_in_admin_and_not_public_until_approved(client, app):
    _class_with_shared_pending_entry(client, app)
    # on the admin dashboard shared queue
    assert b"Shared from" in client.get(f"/admin/{TOKEN}").data
    assert b"Emmy Noether" in client.get(f"/admin/{TOKEN}").data
    # NOT on the public communal pages yet
    assert client.get("/scientists/emmy-noether").status_code == 404
    assert b"Emmy Noether" not in client.get("/textbooks/bk").data


def test_admin_approve_clones_to_independent_communal_entry(client, app):
    tok, slug, eid = _class_with_shared_pending_entry(client, app)
    client.post(f"/admin/{TOKEN}/shared/{eid}/approve")
    # now public
    assert client.get("/scientists/emmy-noether").status_code == 200
    assert b"Emmy Noether" in client.get("/textbooks/bk").data
    conn = _conn(app)
    clone = conn.execute("SELECT * FROM entries WHERE collection_id IS NULL "
                         "AND adopted_from_entry_id=?", (eid,)).fetchone()
    assert clone is not None and clone["communal_status"] == "approved"
    # placement copied
    assert conn.execute("SELECT count(*) FROM placements WHERE entry_id=? AND chapter=3",
                        (clone["id"],)).fetchone()[0] == 1


def test_communal_clone_survives_class_deletion(client, app):
    tok, slug, eid = _class_with_shared_pending_entry(client, app)
    client.post(f"/admin/{TOKEN}/shared/{eid}/approve")
    client.post(f"/manage/{tok}/delete", data={"confirm": slug})
    # class gone, but the public communal copy remains
    assert _conn(app).execute("SELECT count(*) FROM collections").fetchone()[0] == 0
    assert client.get("/scientists/emmy-noether").status_code == 200


def test_admin_reject_keeps_it_off_public_but_on_class_page(client, app):
    tok, slug, eid = _class_with_shared_pending_entry(client, app)
    client.post(f"/admin/{TOKEN}/shared/{eid}/reject")
    assert client.get("/scientists/emmy-noether").status_code == 404
    # still on its own class page (rejection is only about communal sharing)
    assert b"Emmy Noether" in client.get(f"/c/{slug}").data


def test_approve_is_idempotent(client, app):
    tok, slug, eid = _class_with_shared_pending_entry(client, app)
    client.post(f"/admin/{TOKEN}/shared/{eid}/approve")
    client.post(f"/admin/{TOKEN}/shared/{eid}/approve")  # double submit
    n = _conn(app).execute("SELECT count(*) FROM entries WHERE collection_id IS NULL "
                           "AND adopted_from_entry_id=?", (eid,)).fetchone()[0]
    assert n == 1  # not cloned twice


def test_non_shared_entry_cannot_be_communal_approved(client, app):
    # a plain approved entry (not shared) must 404 on the shared route
    loc = client.post("/new", data={"name": "Q", "textbook_mode": "builtin",
                                    "textbook_slug": "bk"}).headers["Location"]
    tok = loc.split("/manage/")[1].split("/")[0]
    slug = _conn(app).execute("SELECT slug FROM collections ORDER BY id DESC LIMIT 1").fetchone()[0]
    client.post(f"/c/{slug}/submit", data={"scientist_name": "Private Person", "chapter": "1",
                "description": "d", "license_grant": "1", "form_ts": "0"})
    eid = _conn(app).execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    client.post(f"/manage/{tok}/entry/{eid}/approve")  # approved but NOT shared
    assert client.post(f"/admin/{TOKEN}/shared/{eid}/approve").status_code == 404


def test_custom_textbook_hidden_from_public_until_it_has_communal_content(client, app):
    # a per-class custom textbook should not appear on public /textbooks
    client.post("/new", data={"name": "Chem", "textbook_mode": "chapters",
                              "custom_title": "Private Chem Book", "chapter_lines": "Atoms\nBonds"})
    assert b"Private Chem Book" not in client.get("/textbooks").data
