"""M4: class collections — create, student-submit, moderate — driven
through the Flask test client (CSRF disabled in TESTING)."""

from pathlib import Path

import pytest

from nonnewtonian import db as db_mod
from nonnewtonian import collections_repo as repo
from nonnewtonian.web import create_app

NOW = "2026-07-07T00:00:00+00:00"
MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textbooks" / "manifest.json"


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "c.db"
    conn = db_mod.init_db(db_path, now=NOW)
    # a builtin textbook so /new has something to pick and placements can match
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
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _conn(app):
    return db_mod.connect(app._db_path)


def _create_class(client, **extra):
    data = {"name": "Physics 121", "textbook_mode": "builtin", "textbook_slug": "bk"}
    data.update(extra)
    r = client.post("/new", data=data)
    assert r.status_code == 302
    return r.headers["Location"]  # /manage/<token>/welcome


def _token_from(location):
    # /manage/<token>/welcome
    return location.split("/manage/")[1].split("/")[0]


def _submit(client, slug, **extra):
    data = {"scientist_name": "Chien-Shiung Wu", "chapter": "3",
            "description": "Overturned parity conservation.", "license_grant": "1",
            "form_ts": "0"}
    data.update(extra)
    return client.post(f"/c/{slug}/submit", data=data)


# --- creation -------------------------------------------------------------

def test_create_class_makes_a_hashed_token(client, app):
    _create_class(client)
    conn = _conn(app)
    row = conn.execute("SELECT manage_token_hash, textbook_id FROM collections").fetchone()
    assert len(row["manage_token_hash"]) == 64  # sha256 hex, not plaintext
    assert row["textbook_id"] is not None


def test_create_requires_name_and_textbook(client):
    assert client.post("/new", data={"name": ""}).status_code == 400
    assert client.post("/new", data={"name": "X", "textbook_mode": "builtin",
                                     "textbook_slug": "nope"}).status_code == 400


def test_create_with_pasted_chapter_list(client, app):
    r = client.post("/new", data={"name": "Chem", "textbook_mode": "chapters",
                                  "custom_title": "My Chem Book",
                                  "chapter_lines": "Atoms\nBonds\nReactions"})
    assert r.status_code == 302
    conn = _conn(app)
    tb = conn.execute("SELECT id FROM textbooks WHERE title='My Chem Book'").fetchone()
    assert conn.execute("SELECT count(*) FROM toc_rows WHERE textbook_id=?", (tb["id"],)).fetchone()[0] == 3


# --- submission + moderation ---------------------------------------------

def test_submission_is_pending_and_not_public(client, app):
    loc = _create_class(client)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    r = _submit(client, slug)
    assert r.status_code == 200 and b"Thanks" in r.data
    # not on the public class page yet
    assert b"Chien-Shiung Wu" not in client.get(f"/c/{slug}").data
    conn = _conn(app)
    assert conn.execute("SELECT status FROM entries").fetchone()[0] == "pending"
    assert conn.execute("SELECT count(*) FROM placements WHERE chapter=3").fetchone()[0] == 1


def test_teacher_approves_and_it_goes_live(client, app):
    loc = _create_class(client)
    token = _token_from(loc)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    _submit(client, slug)
    eid = _conn(app).execute("SELECT id FROM entries").fetchone()[0]
    r = client.post(f"/manage/{token}/entry/{eid}/approve")
    assert r.status_code == 302
    page = client.get(f"/c/{slug}").data
    assert b"Chien-Shiung Wu" in page
    assert b"Chapter 3" in page


def test_reject_keeps_it_off_the_page(client, app):
    loc = _create_class(client)
    token = _token_from(loc)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    _submit(client, slug)
    eid = _conn(app).execute("SELECT id FROM entries").fetchone()[0]
    client.post(f"/manage/{token}/entry/{eid}/reject")
    assert b"Chien-Shiung Wu" not in client.get(f"/c/{slug}").data


def test_duplicate_scientist_is_refused(client, app):
    loc = _create_class(client)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    _submit(client, slug)
    r = _submit(client, slug)  # same scientist again
    assert r.status_code == 400 and b"already submitted" in r.data


def test_missing_license_grant_is_refused(client, app):
    loc = _create_class(client)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    r = client.post(f"/c/{slug}/submit", data={"scientist_name": "X", "chapter": "1",
                    "description": "d", "form_ts": "0"})  # no license_grant
    assert r.status_code == 400 and b"permission to publish" in r.data


def test_honeypot_and_speed_trap(client, app):
    loc = _create_class(client)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    assert _submit(client, slug, website="http://spam").status_code == 400  # honeypot
    import time
    fresh = str(int(time.time()))
    assert _submit(client, slug, scientist_name="Fast Bot", form_ts=fresh).status_code == 400  # too fast


# --- authz ----------------------------------------------------------------

def test_wrong_manage_token_is_404(client, app):
    _create_class(client)
    assert client.get("/manage/not-a-real-token").status_code == 404


def test_moderating_another_class_entry_is_404(client, app):
    # class A
    tokA = _token_from(_create_class(client, name="A"))
    slugA = _conn(app).execute("SELECT slug FROM collections ORDER BY id DESC LIMIT 1").fetchone()[0]
    _submit(client, slugA)
    eidA = _conn(app).execute("SELECT id FROM entries").fetchone()[0]
    # class B's token may not moderate A's entry
    tokB = _token_from(_create_class(client, name="B"))
    assert client.post(f"/manage/{tokB}/entry/{eidA}/approve").status_code == 404


def test_submissions_closed_blocks_submit(client, app):
    tok = _token_from(_create_class(client))
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    client.post(f"/manage/{tok}/settings", data={})  # all toggles off => submissions closed
    assert _submit(client, slug).status_code == 403


def test_attribution_capped_by_class_setting(client, app):
    tok = _token_from(_create_class(client))
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    # class defaults to first_initial ceiling; student asking 'full' is capped
    _submit(client, slug, contributor_name="Ada Lovelace", attribution_mode="full")
    mode = _conn(app).execute("SELECT attribution_mode FROM entries").fetchone()[0]
    assert mode == "first_initial"


def test_delete_does_not_unlink_a_photo_another_class_still_uses(client, app, tmp_path):
    # Two classes, same content-hash photo file: deleting class A must not
    # break class B's page (M4 review critical).
    import nonnewtonian.web.views_class as vc
    photo_dir = tmp_path / "photos"; photo_dir.mkdir(exist_ok=True)
    shared = photo_dir / "ab"; shared.mkdir()
    (shared / "hash.jpg").write_bytes(b"img")
    rel = "ab/hash.jpg"

    tokA = _token_from(_create_class(client, name="A"))
    slugA = _conn(app).execute("SELECT slug FROM collections ORDER BY id DESC LIMIT 1").fetchone()[0]
    tokB = _token_from(_create_class(client, name="B"))
    conn = _conn(app)
    for slug in (slugA,):
        cid = conn.execute("SELECT id FROM collections WHERE slug=?", (slug,)).fetchone()[0]
    # give each class an entry pointing at the SAME file
    for cid in [r[0] for r in conn.execute("SELECT id FROM collections")]:
        conn.execute("INSERT INTO entries(collection_id,scientist_name,scientist_slug,created_at,updated_at) "
                     "VALUES(?,'S','s',?,?)", (cid, NOW, NOW))
        eid = conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO photos(entry_id,file_path,is_primary,fetch_status) VALUES(?,?,1,'stored')", (eid, rel))
    conn.commit(); conn.close()

    client.post(f"/manage/{tokA}/delete", data={"confirm": slugA})
    assert (shared / "hash.jpg").exists()  # class B still references it -> file kept


def test_slug_fallback_keeps_distinct_nonlatin_names_distinct():
    from nonnewtonian.slugs import slugify
    a, b = slugify("吴健雄"), slugify("李政道")
    assert a != b and a != "x" and b != "x"
    assert slugify("吴健雄") == a  # stable


def test_two_nonlatin_scientists_are_not_false_duplicates(client, app):
    loc = _create_class(client)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    assert _submit(client, slug, scientist_name="吴健雄").status_code == 200
    assert _submit(client, slug, scientist_name="李政道").status_code == 200  # distinct, not "already submitted"


def test_manage_page_is_frame_denied_but_embed_is_framable(client, app):
    tok = _token_from(_create_class(client))
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    assert client.get(f"/manage/{tok}").headers.get("X-Frame-Options") == "DENY"
    embed = client.get(f"/c/{slug}/embed")
    assert embed.headers.get("X-Frame-Options") is None
    assert "frame-ancestors" in embed.headers.get("Content-Security-Policy", "")


def test_no_referrer_policy_on_manage(client, app):
    tok = _token_from(_create_class(client))
    assert client.get(f"/manage/{tok}").headers.get("Referrer-Policy") == "no-referrer"


def test_submission_without_valid_chapter_is_rejected(client, app):
    loc = _create_class(client)
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    # class has a textbook; a bogus chapter would place nowhere -> reject,
    # don't accept an entry that could be approved yet stay invisible.
    r = client.post(f"/c/{slug}/submit", data={"scientist_name": "Ghost", "chapter": "999",
                    "description": "d", "license_grant": "1", "form_ts": "0"})
    assert r.status_code == 400 and b"choose a chapter" in r.data


def test_delete_requires_slug_confirmation(client, app):
    tok = _token_from(_create_class(client))
    slug = _conn(app).execute("SELECT slug FROM collections").fetchone()[0]
    assert client.post(f"/manage/{tok}/delete", data={"confirm": "wrong"}).status_code == 400
    r = client.post(f"/manage/{tok}/delete", data={"confirm": slug})
    assert r.status_code == 200 and b"has been deleted" in r.data
    assert _conn(app).execute("SELECT count(*) FROM collections").fetchone()[0] == 0
