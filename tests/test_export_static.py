"""M7 WP10: static export of the public communal site.

Built on the real 39-file seed corpus, plus class, shared, pending and
rejected content and photos, so the leak guard is exercised against the
kinds of rows production holds."""

import csv
import hashlib
import io
import re
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from nonnewtonian import db as db_mod
from nonnewtonian import export_static as ex
from nonnewtonian.cli import main as cli_main
from nonnewtonian.importer import seed_import
from nonnewtonian.photos import store_bytes
from nonnewtonian.web import create_app

from conftest import SCIENTISTS

NOW = "2026-07-07T00:00:00+00:00"
TEXTBOOKS = Path(__file__).resolve().parents[1] / "data" / "textbooks"
TOKEN = "export-test-admin-token"
SITE = "https://nnp.example.org"
CLASS_ONLY_STUDENT = "Xavier Hiddenname"
SHARED_STUDENT = "Yolanda Secretname"
TEACHER_EMAIL = "teacher-export@example.edu"


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def _add_photo(conn, photo_dir, entry_id, color) -> str:
    stored = store_bytes(_png(color), photo_dir)
    rel = str(stored.path.relative_to(photo_dir))
    conn.execute(
        "INSERT INTO photos(entry_id,file_path,sha256,is_primary,fetch_status,license,"
        "license_verified) VALUES(?,?,?,1,'stored','CC0',1)", (entry_id, rel, stored.sha256))
    conn.commit()
    return rel


def _entry_id(conn, slug, communal=True):
    sql = "SELECT id FROM entries WHERE scientist_slug=? AND collection_id IS " + (
        "NULL" if communal else "NOT NULL")
    return conn.execute(sql, (slug,)).fetchone()[0]


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A seeded DB with approved seeds plus every kind of non-public content."""
    # The export builds its own app from the environment, as under systemd.
    monkeypatch.setenv("NNP_ADMIN_TOKEN", TOKEN)
    db_path, photo_dir = tmp_path / "app.db", (tmp_path / "photos").resolve()
    conn = db_mod.init_db(db_path, now=NOW)
    seed_import(conn, scientists_dir=SCIENTISTS, textbooks_dir=TEXTBOOKS,
                photo_dir=photo_dir, now=NOW, decks_dir=None, fetch_photos=False)
    photos = {
        "public": _add_photo(conn, photo_dir, _entry_id(conn, "emmy-noether"), (200, 10, 10)),
        "rejected": _add_photo(conn, photo_dir, _entry_id(conn, "lise-meitner"), (10, 200, 10)),
    }
    conn.close()
    app = create_app({"DB_PATH": str(db_path), "PHOTO_DIR": str(photo_dir),
                      "ADMIN_TOKEN": TOKEN, "TESTING": True, "WTF_CSRF_ENABLED": False})
    c = app.test_client()
    conn = db_mod.connect(db_path)
    c.post(f"/admin/{TOKEN}/entry/{_entry_id(conn, 'lise-meitner')}/reject")
    c.post(f"/admin/{TOKEN}/approve-all-seeds")
    # A communal entry still awaiting review.
    conn.execute(
        "INSERT INTO entries(collection_id,scientist_name,scientist_slug,description,"
        "status,communal_status,created_at,updated_at) VALUES(NULL,'Penelope Pendingseed',"
        "'penelope-pendingseed','Not reviewed yet.','pending','pending',?,?)", (NOW, NOW))
    conn.commit()

    loc = c.post("/new", data={"name": "Export Class", "textbook_mode": "builtin",
                               "textbook_slug": "knight-calc-3rd", "share_default": "1",
                               "teacher_email": TEACHER_EMAIL}).headers["Location"]
    mtok = loc.split("/manage/")[1].split("/")[0]
    cslug = conn.execute("SELECT slug FROM collections").fetchone()[0]

    def submit(name, student, color):
        c.post(f"/c/{cslug}/submit", data={
            "scientist_name": name, "chapter": "3", "description": f"About {name}.",
            "license_grant": "1", "form_ts": "0", "contributor_name": student,
            "attribution_mode": "first_initial",
            "photo_file": (io.BytesIO(_png(color)), "p.png")},
            content_type="multipart/form-data")
        return _entry_id(conn, re.sub(r"\W+", "-", name.lower()), communal=False)

    class_only = submit("Quorra Classonly", CLASS_ONLY_STUDENT, (10, 10, 200))
    c.post(f"/manage/{mtok}/entry/{class_only}/approve")
    shared = submit("Wilhelmina Sharedperson", SHARED_STUDENT, (200, 200, 10))
    c.post(f"/manage/{mtok}/entry/{shared}/approve", data={"share_communal": "1"})
    c.post(f"/admin/{TOKEN}/shared/{shared}/approve")
    submit("Pending Classperson", "Zachary Waitingname", (10, 200, 200))
    photos["class_only"] = conn.execute(
        "SELECT file_path FROM photos WHERE entry_id=?", (class_only,)).fetchone()[0]
    photos["shared"] = conn.execute(
        "SELECT file_path FROM photos WHERE entry_id=?", (shared,)).fetchone()[0]
    conn.close()
    assert c.get("/scientists/wilhelmina-sharedperson").status_code == 200
    return {"db": db_path, "photos": photo_dir, "client": c, "app": app, "slug": cslug,
            "photo": photos, "out": tmp_path / "export" / "current"}


def _export(site):
    return ex.export_site(site["db"], site["photos"], site["out"], SITE)


def _all_text(out: Path) -> str:
    chunks = []
    for p in sorted(out.rglob("*")):
        if p.suffix in (".html", ".csv", ".css"):
            chunks.append(p.read_text("utf-8"))
        elif p.suffix == ".pptx":
            with zipfile.ZipFile(p) as z:
                chunks += [z.read(n).decode("utf-8", "replace") for n in z.namelist()
                           if n.endswith(".xml")]
    return "\n".join(chunks)


def _csv_rows(out: Path):
    with open(out / "entries.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_export_layout_and_counts(site):
    report = _export(site)
    out = site["out"]
    assert report.entries == len(_csv_rows(out)) >= 39
    assert report.photos == 2  # Noether's seed photo + the promoted clone's
    for rel in ["index.html", "scientists/index.html", "textbooks/index.html",
                "wanted/index.html", "about/index.html", "for-teachers/index.html",
                "for-students/index.html", "privacy/index.html", "static/style.css",
                "entries.csv", ".nojekyll", "down.html",
                "scientists/emmy-noether/index.html", "scientists/emmy-noether/slide.pptx",
                "textbooks/knight-calc-3rd/index.html"]:
        assert (out / rel).is_file(), rel
    assert (out / "scientists/emmy-noether/slide.pptx").read_bytes()[:2] == b"PK"
    home = (out / "index.html").read_text()
    assert 'class="mirror-note"' in home and "read-only copy" in home
    assert f'<link rel="canonical" href="{SITE}/">' in home
    assert not Path(str(out) + ".next").exists() and not Path(str(out) + ".prev").exists()


def test_only_public_content_is_exported(site):
    _export(site)
    out = site["out"]
    text = _all_text(out)
    for absent in ["Lise Meitner", "Penelope Pendingseed", "Quorra Classonly",
                   "Pending Classperson", CLASS_ONLY_STUDENT, SHARED_STUDENT,
                   "Zachary Waitingname", TEACHER_EMAIL, site["slug"], TOKEN, "csrf_token"]:
        assert absent not in text, absent
    slugs = {r["slug"] for r in _csv_rows(out)}
    assert {"lise-meitner", "penelope-pendingseed", "quorra-classonly",
            "pending-classperson"}.isdisjoint(slugs)
    assert "wilhelmina-sharedperson" in slugs and "emmy-noether" in slugs
    assert not (out / "c").exists() and not (out / "scientists" / "lise-meitner").exists()
    exported_photos = {p.relative_to(out / "photos").as_posix()
                       for p in (out / "photos").rglob("*") if p.is_file()}
    assert exported_photos == {site["photo"]["public"], site["photo"]["shared"]}


def test_csv_uses_displayed_credit_not_contributor_name(site):
    _export(site)
    out = site["out"]
    raw = (out / "entries.csv").read_text("utf-8")
    assert SHARED_STUDENT not in raw
    row = next(r for r in _csv_rows(out) if r["slug"] == "wilhelmina-sharedperson")
    assert row["credited_as"] == "Yolanda S."
    assert row["photo_file"] == f"photos/{site['photo']['shared']}"
    assert list(_csv_rows(out)[0].keys()) == ex.CSV_COLUMNS
    slugs = [r["slug"] for r in _csv_rows(out)]
    assert slugs == sorted(slugs)


def test_two_exports_are_byte_identical(site, tmp_path):
    first = tmp_path / "a" / "current"
    second = tmp_path / "b" / "current"
    ex.export_site(site["db"], site["photos"], first, SITE)
    time.sleep(2.1)  # past the 2s resolution of zip timestamps
    ex.export_site(site["db"], site["photos"], second, SITE)

    def digests(root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*") if p.is_file()}

    a, b = digests(first), digests(second)
    assert a == b
    assert any(k.endswith(".pptx") for k in a)


def test_links_resolve_inside_the_export(site):
    _export(site)
    out = site["out"]
    attr = re.compile(r'\b(href|src|action)="([^"]*)"')
    deck_links = 0
    for page in out.rglob("*.html"):
        for kind, value in attr.findall(page.read_text("utf-8")):
            assert not (value.startswith("/") and not value.startswith("//")), (page, value)
            if kind == "action":
                assert value.startswith(SITE + "/"), (page, value)
            if value.endswith("deck.pptx"):
                assert value.startswith(SITE + "/textbooks/"), value
                deck_links += 1
            if re.match(r"^[a-z][a-z0-9+.-]*:|^#", value):
                continue  # absolute / mailto / fragment
            target = (page.parent / value.split("#")[0]).resolve()
            assert target.is_file(), (page, value)
            assert out.resolve() in target.parents, (page, value)
    assert deck_links >= 1
    tb = (out / "textbooks/knight-calc-3rd/index.html").read_text()
    assert 'href="../../scientists/emmy-noether/index.html"' in tb
    assert 'href="../../scientists/emmy-noether/slide.pptx"' in tb
    assert 'href="../../static/style.css"' in tb


def test_leak_guard_raises_and_keeps_previous_export(site, monkeypatch):
    _export(site)
    out = site["out"]
    before = (out / "index.html").read_bytes()
    from nonnewtonian.web import views_public
    real = views_public.render_template

    def leaky(*a, **kw):
        return real(*a, **kw) + f"<!-- {TOKEN} -->"

    monkeypatch.setattr(views_public, "render_template", leaky)
    with pytest.raises(ex.ExportError, match="ADMIN_TOKEN"):
        ex.export_site(site["db"], site["photos"], out, SITE)
    assert (out / "index.html").read_bytes() == before
    assert not Path(str(out) + ".next").exists()


def test_leak_guard_catches_a_hidden_contributor_name(site, monkeypatch):
    # Simulates a template bug that prints contributor_name instead of
    # displayed_credit() on a public page.
    from nonnewtonian.web import views_public
    real = views_public.render_template
    monkeypatch.setattr(views_public, "render_template",
                        lambda *a, **kw: real(*a, **kw) + SHARED_STUDENT)
    with pytest.raises(ex.ExportError, match="contributor name"):
        _export(site)
    assert not site["out"].exists()


def test_leak_guard_catches_a_class_link(site, monkeypatch):
    from nonnewtonian.web import views_public
    real = views_public.render_template
    monkeypatch.setattr(views_public, "render_template",
                        lambda *a, **kw: real(*a, **kw) + '<a href="/c/anything">x</a>')
    with pytest.raises(ex.ExportError, match="links to c"):
        _export(site)


def test_zero_entries_raises(tmp_path):
    db_path = tmp_path / "empty.db"
    db_mod.init_db(db_path, now=NOW).close()
    with pytest.raises(ex.ExportError, match="no approved entries"):
        ex.export_site(db_path, tmp_path / "photos", tmp_path / "out", SITE)
    assert not (tmp_path / "out").exists()


def test_missing_db_raises_without_creating_it(tmp_path):
    with pytest.raises(ex.ExportError, match="database missing"):
        ex.export_site(tmp_path / "nope.db", tmp_path / "photos", tmp_path / "out", SITE)
    assert not (tmp_path / "nope.db").exists()


def test_takedown_removes_page_slide_row_and_photo(site):
    _export(site)
    out = site["out"]
    rel = site["photo"]["public"]
    assert (out / "photos" / rel).is_file()
    conn = db_mod.connect(site["db"])
    eid = _entry_id(conn, "emmy-noether")
    conn.close()
    site["client"].post(f"/admin/{TOKEN}/communal/{eid}/delete")
    _export(site)
    assert not (out / "scientists" / "emmy-noether").exists()
    assert "emmy-noether" not in {r["slug"] for r in _csv_rows(out)}
    assert not (out / "photos" / rel).exists()
    assert "Emmy Noether" not in _all_text(out)


def test_cli_export_static(site, capsys):
    rc = cli_main(["export-static", "--db", str(site["db"]), "--photos", str(site["photos"]),
                   "--out", str(site["out"]), "--site-url", SITE + "/"])
    assert rc == 0 and (site["out"] / "index.html").is_file()
    assert "exported" in capsys.readouterr().out
    rc = cli_main(["export-static", "--db", str(site["out"] / "missing.db"),
                   "--photos", str(site["photos"]), "--out", str(site["out"]),
                   "--site-url", SITE])
    assert rc == 1 and "database missing" in capsys.readouterr().err


def test_slide_notes_use_displayed_credit(site):
    # The per-entry slide is exported publicly: its speaker notes must not
    # carry a first_initial contributor's full name.
    data = site["client"].get("/scientists/wilhelmina-sharedperson/slide.pptx").data
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        notes = "".join(z.read(n).decode() for n in z.namelist() if "notesSlide" in n)
    assert "Yolanda S." in notes and SHARED_STUDENT not in notes


def _add_hidden_contributor(db_path, name, slug="hidden-contrib", status="pending"):
    conn = db_mod.connect(db_path)
    conn.execute(
        "INSERT INTO entries(collection_id,scientist_name,scientist_slug,description,status,"
        "communal_status,contributor_name,attribution_mode,created_at,updated_at) "
        "VALUES(NULL,'Hidden Contrib',?,'x',?,?,?,'anonymous',?,?)",
        (slug, status, status, name, NOW, NOW))
    conn.commit()
    conn.close()


def test_short_hidden_name_that_is_a_word_prefix_does_not_stop_export(site):
    # M7 review #10: 'Mark' used to match the footer's "some are marked".
    _add_hidden_contributor(site["db"], "Mark")
    assert "marked" in site["client"].get("/about").get_data(as_text=True)
    _export(site)
    assert (site["out"] / "about" / "index.html").is_file()


def test_hidden_name_as_a_whole_word_still_trips_the_guard(site, monkeypatch):
    _add_hidden_contributor(site["db"], "Mark")
    from nonnewtonian.web import views_public
    real = views_public.render_template
    monkeypatch.setattr(views_public, "render_template",
                        lambda *a, **kw: real(*a, **kw) + "<p>by Mark.</p>")
    with pytest.raises(ex.ExportError, match="contributor name of entry"):
        _export(site)


def test_leak_guard_catches_an_apostrophe_name_as_jinja_escapes_it(site, monkeypatch):
    # M7 review #29: Jinja writes ' as &#39;, which html.escape spells &#x27;.
    from markupsafe import escape
    _add_hidden_contributor(site["db"], "Sean O'Brien")
    from nonnewtonian.web import views_public
    real = views_public.render_template
    monkeypatch.setattr(views_public, "render_template",
                        lambda *a, **kw: real(*a, **kw) + str(escape("Sean O'Brien")))
    with pytest.raises(ex.ExportError, match="contributor name of entry"):
        _export(site)


def test_junk_teacher_email_already_in_the_db_does_not_stop_export(site):
    # M7 review #12: a pre-validation 'e' used to match every page.
    conn = db_mod.connect(site["db"])
    conn.execute("UPDATE collections SET teacher_email='e'")
    conn.commit()
    conn.close()
    _export(site)


def test_leak_guard_names_the_collection_of_a_leaked_teacher_email(site, monkeypatch):
    from nonnewtonian.web import views_public
    real = views_public.render_template
    monkeypatch.setattr(views_public, "render_template",
                        lambda *a, **kw: real(*a, **kw) + TEACHER_EMAIL)
    with pytest.raises(ex.ExportError, match=r"teacher email of collection \d+"):
        _export(site)
