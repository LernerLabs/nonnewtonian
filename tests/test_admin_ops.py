"""M7 WP4: admin re-issue of a class manage link (no SMTP needed) and
admin takedown of ANY entry, class or communal, with refcounted photo
cleanup."""

import re

import pytest

from nonnewtonian import collections_repo as repo
from nonnewtonian import db as db_mod
from nonnewtonian import tokens
from nonnewtonian.web import create_app

NOW = "2026-07-07T00:00:00+00:00"
TOKEN = "admin-tok"


def _make_app(tmp_path, **extra):
    db_path = tmp_path / "m7.db"
    conn = db_mod.init_db(db_path, now=NOW)
    conn.execute("INSERT INTO textbooks(slug,title,edition,is_builtin,created_at) "
                 "VALUES('bk','The Book','1st',1,?)", (NOW,))
    tid = conn.execute("SELECT id FROM textbooks WHERE slug='bk'").fetchone()[0]
    for ch in range(1, 6):
        conn.execute("INSERT INTO toc_rows(textbook_id,sort_order,chapter,topics) "
                     "VALUES(?,?,?,?)", (tid, ch, ch, f"Topic {ch}"))
    conn.commit(); conn.close()
    cfg = {"DB_PATH": str(db_path), "PHOTO_DIR": str(tmp_path / "photos"),
           "ADMIN_TOKEN": TOKEN, "TESTING": True, "WTF_CSRF_ENABLED": False}
    cfg.update(extra)
    app = create_app(cfg)
    app._db_path = str(db_path)
    return app


@pytest.fixture
def app(tmp_path):
    return _make_app(tmp_path)


@pytest.fixture
def client(app):
    return app.test_client()


def _conn(app):
    return db_mod.connect(app._db_path)


def _new_class(client, app, name="P121"):
    loc = client.post("/new", data={"name": name, "textbook_mode": "builtin",
                                    "textbook_slug": "bk", "share_default": "1",
                                    "teacher_email": "t@example.edu"}).headers["Location"]
    tok = loc.split("/manage/")[1].split("/")[0]
    row = _conn(app).execute("SELECT id, slug FROM collections ORDER BY id DESC LIMIT 1").fetchone()
    return tok, row["id"], row["slug"]


def _submit(client, app, slug, scientist):
    client.post(f"/c/{slug}/submit", data={"scientist_name": scientist, "chapter": "3",
                "description": "d", "license_grant": "1", "form_ts": "0"})
    return _conn(app).execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]


def _add_photo(app, tmp_path, entry_id, relpath):
    f = tmp_path / "photos" / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    conn = _conn(app)
    conn.execute("INSERT INTO photos(entry_id,file_path,is_primary,fetch_status) "
                 "VALUES(?,?,1,'stored')", (entry_id, relpath))
    conn.commit(); conn.close()
    return f


def _expected(app, cid) -> dict:
    """The re-issue form's hidden CAS field, as the admin page renders it."""
    h = _conn(app).execute("SELECT manage_token_hash FROM collections WHERE id=?",
                           (cid,)).fetchone()[0]
    return {"expected": h[:16]}


def _manage_token_from(html: str) -> str:
    m = re.search(r'value="[^"]*/manage/([^"/]+)" id="manage-url"', html)
    assert m, html
    return m.group(1)


# --- re-issue ---------------------------------------------------------------

def test_admin_page_lists_classes(client, app):
    _, _, slug = _new_class(client, app)
    page = client.get(f"/admin/{TOKEN}").get_data(as_text=True)
    assert "P121" in page and slug in page and "t@example.edu" in page
    assert "Re-issue manage link" in page


def test_reissue_rotates_manage_link(client, app):
    old, cid, _ = _new_class(client, app)
    assert client.get(f"/manage/{old}").status_code == 200
    r = client.post(f"/admin/{TOKEN}/collections/{cid}/reissue", data=_expected(app, cid))
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Shown once. The old link is already dead." in html
    assert "mailto:t@example.edu" in html
    new = _manage_token_from(html)
    assert new != old
    assert client.get(f"/manage/{old}").status_code == 404
    assert client.get(f"/manage/{new}").status_code == 200
    assert client.get(f"/manage/{new}/welcome").status_code == 200
    # only the hash is stored
    stored = _conn(app).execute("SELECT manage_token_hash FROM collections WHERE id=?",
                                (cid,)).fetchone()[0]
    assert stored == tokens.hash_token(new) and new not in stored


def test_reissue_resubmit_is_stale_not_a_second_rotation(client, app):
    # M7 review #33: a reload / Back+resubmit of the show-once page re-POSTs
    # the same form; it must not silently kill the link just sent.
    _, cid, _ = _new_class(client, app)
    page = client.get(f"/admin/{TOKEN}").get_data(as_text=True)
    m = re.search(r'name="expected" value="([0-9a-f]{16})"', page)
    assert m, page
    form = {"expected": m.group(1)}
    first = client.post(f"/admin/{TOKEN}/collections/{cid}/reissue", data=form)
    sent = _manage_token_from(first.get_data(as_text=True))
    again = client.post(f"/admin/{TOKEN}/collections/{cid}/reissue", data=form)
    assert again.status_code == 302 and f"reissue_stale={cid}" in again.headers["Location"]
    assert client.get(f"/manage/{sent}").status_code == 200
    banner = client.get(again.headers["Location"]).get_data(as_text=True)
    assert "Not re-issued" in banner
    # a form with no CAS field changes nothing either
    assert client.post(f"/admin/{TOKEN}/collections/{cid}/reissue").status_code == 302
    assert client.get(f"/manage/{sent}").status_code == 200


def test_reissue_get_not_allowed(client, app):
    _, cid, _ = _new_class(client, app)
    assert client.get(f"/admin/{TOKEN}/collections/{cid}/reissue").status_code == 405


def test_reissue_wrong_admin_token_404_and_link_unchanged(client, app):
    old, cid, _ = _new_class(client, app)
    assert client.post(f"/admin/wrong/collections/{cid}/reissue").status_code == 404
    assert client.get(f"/manage/{old}").status_code == 200


def test_reissue_unknown_class_404(client, app):
    assert client.post(f"/admin/{TOKEN}/collections/999/reissue").status_code == 404


def test_reissue_cas_returns_none_on_stale_hash(client, app):
    old, cid, _ = _new_class(client, app)
    conn = _conn(app)
    h = tokens.hash_token(old)
    t1 = repo.reissue_manage_token(conn, cid, expected_hash=h)
    assert t1 is not None
    # second use of the same (now stale) hash loses the compare-and-swap
    assert repo.reissue_manage_token(conn, cid, expected_hash=h) is None
    stored = conn.execute("SELECT manage_token_hash FROM collections WHERE id=?",
                          (cid,)).fetchone()[0]
    assert stored == tokens.hash_token(t1)


def test_reissue_forms_carry_csrf_token(tmp_path):
    app = _make_app(tmp_path, WTF_CSRF_ENABLED=True)
    client = app.test_client()
    conn = _conn(app)
    created = repo.create_collection(conn, name="C", teacher_name=None, teacher_email=None,
                                     textbook_id=None, now=NOW)
    page = client.get(f"/admin/{TOKEN}").get_data(as_text=True)
    form = re.search(r'action="/admin/%s/collections/%d/reissue"[^>]*>\s*'
                     r'<input type="hidden" name="csrf_token" value="([^"]+)"'
                     % (TOKEN, created.id), page)
    assert form, page
    assert client.post(f"/admin/{TOKEN}/collections/{created.id}/reissue").status_code == 400
    r = client.post(f"/admin/{TOKEN}/collections/{created.id}/reissue",
                    data={"csrf_token": form.group(1), **_expected(app, created.id)})
    assert r.status_code == 200
    take = re.search(r'action="/admin/%s/takedown"[^>]*>\s*'
                     r'<input type="hidden" name="csrf_token"' % TOKEN, page)
    assert take, page


# --- takedown ---------------------------------------------------------------

def test_takedown_class_entry_keeps_photo_shared_with_communal_clone(client, app, tmp_path):
    tok, _, slug = _new_class(client, app)
    shared_eid = _submit(client, app, slug, "Emmy Noether")
    shared_file = _add_photo(app, tmp_path, shared_eid, "ab/shared.jpg")
    client.post(f"/manage/{tok}/entry/{shared_eid}/approve", data={"share_communal": "1"})
    client.post(f"/admin/{TOKEN}/shared/{shared_eid}/approve")  # clone references the file
    lone_eid = _submit(client, app, slug, "Lise Meitner")
    lone_file = _add_photo(app, tmp_path, lone_eid, "cd/lone.jpg")

    r = client.post(f"/admin/{TOKEN}/takedown", data={"entry_id": str(lone_eid)})
    assert r.status_code == 302 and f"took_down={lone_eid}" in r.headers["Location"]
    assert not lone_file.exists()  # orphan reclaimed
    assert _conn(app).execute("SELECT count(*) FROM entries WHERE id=?",
                              (lone_eid,)).fetchone()[0] == 0

    r = client.post(f"/admin/{TOKEN}/takedown", data={"entry_id": str(shared_eid)})
    assert r.status_code == 302
    assert _conn(app).execute("SELECT count(*) FROM entries WHERE id=?",
                              (shared_eid,)).fetchone()[0] == 0
    assert shared_file.exists()  # still referenced by the communal clone
    assert client.get("/scientists/emmy-noether").status_code == 200
    assert f"Entry {shared_eid} taken down." in client.get(
        r.headers["Location"]).get_data(as_text=True)


def test_takedown_communal_entry(client, app, tmp_path):
    tok, _, slug = _new_class(client, app)
    eid = _submit(client, app, slug, "Emmy Noether")
    client.post(f"/manage/{tok}/entry/{eid}/approve", data={"share_communal": "1"})
    client.post(f"/admin/{TOKEN}/shared/{eid}/approve")
    clone_id = _conn(app).execute(
        "SELECT id FROM entries WHERE collection_id IS NULL AND adopted_from_entry_id=?",
        (eid,)).fetchone()[0]
    assert client.get("/scientists/emmy-noether").status_code == 200
    assert client.post(f"/admin/{TOKEN}/takedown",
                       data={"entry_id": str(clone_id)}).status_code == 302
    assert client.get("/scientists/emmy-noether").status_code == 404
    # the old communal-only route still works too (refactored onto the same helper)
    eid2 = _submit(client, app, slug, "Lise Meitner")
    client.post(f"/manage/{tok}/entry/{eid2}/approve", data={"share_communal": "1"})
    client.post(f"/admin/{TOKEN}/shared/{eid2}/approve")
    clone2 = _conn(app).execute(
        "SELECT id FROM entries WHERE collection_id IS NULL AND adopted_from_entry_id=?",
        (eid2,)).fetchone()[0]
    assert client.post(f"/admin/{TOKEN}/communal/{clone2}/delete").status_code == 302
    assert client.get("/scientists/lise-meitner").status_code == 404


def test_takedown_rejects_bad_requests(client, app):
    assert client.post(f"/admin/{TOKEN}/takedown", data={"entry_id": "999"}).status_code == 404
    assert client.post(f"/admin/{TOKEN}/takedown", data={"entry_id": "abc"}).status_code == 404
    assert client.post(f"/admin/{TOKEN}/takedown", data={}).status_code == 404
    assert client.get(f"/admin/{TOKEN}/takedown").status_code == 405


def test_takedown_wrong_admin_token_404(client, app):
    _, _, slug = _new_class(client, app)
    eid = _submit(client, app, slug, "Emmy Noether")
    assert client.post("/admin/wrong/takedown", data={"entry_id": str(eid)}).status_code == 404
    assert _conn(app).execute("SELECT count(*) FROM entries WHERE id=?",
                              (eid,)).fetchone()[0] == 1


@pytest.mark.parametrize("name", ["Miles O'Brien", "X');fetch('/x');('",
                                  'A "quoted" <b>name</b>'])
def test_published_takedown_confirm_is_a_json_string(client, app, name):
    # M7 review #13: HTML escaping alone let ' close the JS string once the
    # browser decoded the attribute.
    import html as _html
    import json
    tok, _, slug = _new_class(client, app)
    eid = _submit(client, app, slug, name)
    client.post(f"/manage/{tok}/entry/{eid}/approve", data={"share_communal": "1"})
    client.post(f"/admin/{TOKEN}/shared/{eid}/approve")
    page = client.get(f"/admin/{TOKEN}").get_data(as_text=True)
    handlers = [_html.unescape(h) for h in re.findall(r'onsubmit="([^"]*)"', page)
                if "communal site" in _html.unescape(h)]
    assert len(handlers) == 1, page
    m = re.fullmatch(r"return confirm\((.*)\);", handlers[0])
    assert m, handlers[0]
    assert json.loads(m.group(1)) == f"Take down {name} from the communal site?"
