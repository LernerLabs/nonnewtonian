"""Seed-importer acceptance tests over the real 39-file corpus.

Run with fetch_photos=False so they are deterministic and offline; the
photo pipeline itself is covered in test_photos.py.
"""

import json
from pathlib import Path

import pytest

from nonnewtonian import db as db_mod
from nonnewtonian.importer import seed_import

from conftest import FIXTURES, SCIENTISTS

NOW = "2026-07-07T00:00:00+00:00"
MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textbooks" / "manifest.json"


@pytest.fixture
def seeded(tmp_path):
    conn = db_mod.init_db(tmp_path / "seed.db", now=NOW)
    report = seed_import(
        conn, scientists_dir=SCIENTISTS, manifest_path=MANIFEST,
        photo_dir=tmp_path / "photos", now=NOW, decks_dir=None, fetch_photos=False,
    )
    return conn, report


def _count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_three_textbooks_and_toc_rows(seeded):
    conn, report = seeded
    assert _count(conn, "SELECT count(*) FROM textbooks") == 3
    assert _count(conn, "SELECT count(*) FROM toc_rows") == 95  # 42 + 30 + 23
    assert set(report.textbooks_loaded) == {"knight-calc-3rd", "knight-college-2nd", "mandi-4th"}
    assert report.textbooks_skipped == []


def test_all_entries_are_communal_pending(seeded):
    conn, _ = seeded
    total = _count(conn, "SELECT count(*) FROM entries")
    assert total == 41  # 39 files, 2 split into 2 writeups each
    assert _count(
        conn,
        "SELECT count(*) FROM entries WHERE collection_id IS NULL "
        "AND status='pending' AND communal_status='pending'",
    ) == total


def test_placements_nothing_dropped(seeded):
    conn, report = seeded
    assert _count(conn, "SELECT count(*) FROM placements") == 65
    assert report.total_placements == 65
    # 3 genuinely-unknown-textbook lines kept, textbook_id NULL, raw preserved
    unassigned = conn.execute(
        "SELECT raw_line FROM placements WHERE textbook_id IS NULL"
    ).fetchall()
    assert len(unassigned) == 3
    assert all(row["raw_line"].strip() for row in unassigned)
    # both Halliday lines survive
    halliday = [r["raw_line"] for r in unassigned if "Halliday" in r["raw_line"]]
    assert len(halliday) == 2


def test_multi_student_split_places_scientist_facts_once(seeded):
    conn, _ = seeded
    # Chien-Shiung Wu: 2 writeups, but her placements exist once.
    assert _count(conn, "SELECT count(*) FROM entries WHERE scientist_slug='chien-shiung-wu'") == 2
    wu_placements = _count(
        conn,
        "SELECT count(*) FROM placements p JOIN entries e ON p.entry_id=e.id "
        "WHERE e.scientist_slug='chien-shiung-wu'",
    )
    assert wu_placements == 2  # not 4
    # both writeups flagged
    flags = [json.loads(r["review_flags"]) for r in conn.execute(
        "SELECT review_flags FROM entries WHERE scientist_slug='chien-shiung-wu'")]
    assert all("split-multi-student" in f for f in flags)


def test_name_corrections_applied_with_original_preserved(seeded):
    conn, _ = seeded
    kaku = conn.execute(
        "SELECT scientist_name, scientist_slug, review_flags FROM entries "
        "WHERE seed_origin='MichioKatu.txt'").fetchone()
    assert kaku["scientist_name"] == "Michio Kaku"
    assert kaku["scientist_slug"] == "michio-kaku"
    assert "name-corrected-from:Michio Katu" in json.loads(kaku["review_flags"])


def test_emmy_noether_two_chapter_placements(seeded):
    conn, _ = seeded
    chapters = sorted(r["chapter"] for r in conn.execute(
        "SELECT chapter FROM placements p JOIN entries e ON p.entry_id=e.id "
        "WHERE e.scientist_slug='emmy-noether'"))
    assert chapters == [9, 10]


def test_knight_2nd_shorthand_now_matched(seeded):
    conn, _ = seeded
    # Chien-Shiung Wu's 'Knight, 2nd edition ... Chapter 30' now resolves.
    row = conn.execute(
        "SELECT p.chapter, t.slug FROM placements p "
        "JOIN entries e ON p.entry_id=e.id "
        "JOIN textbooks t ON p.textbook_id=t.id "
        "WHERE e.scientist_slug='chien-shiung-wu' AND t.slug='knight-college-2nd'"
    ).fetchone()
    assert row is not None and row["chapter"] == 30


def test_photos_rows_created_with_unverified_license(seeded):
    conn, _ = seeded
    # 41 original photo URLs (no fetching in tests, so file_path is NULL).
    assert _count(conn, "SELECT count(*) FROM photos") == 41
    assert _count(conn, "SELECT count(*) FROM photos WHERE license_verified=1") == 0
    # attribution defaults to the origin URL
    row = conn.execute(
        "SELECT original_url, attribution, license FROM photos WHERE original_url IS NOT NULL LIMIT 1"
    ).fetchone()
    assert row["attribution"] == row["original_url"]
    assert "unverified" in row["license"]


def test_wikipedia_verbatim_gets_license_notice(seeded):
    conn, _ = seeded
    row = conn.execute(
        "SELECT license_notice, review_flags FROM entries WHERE seed_origin='LiseMeitner.txt'"
    ).fetchone()
    assert row["license_notice"] and "BY-SA" in row["license_notice"]
    assert "verbatim-from-source" in json.loads(row["review_flags"])


def test_wanted_list_seeded(seeded):
    conn, report = seeded
    assert report.wanted_loaded == 6  # Ibn al-Haytham removed (he has an entry)
    assert _count(conn, "SELECT count(*) FROM wanted_scientists") == 6
    # nobody in the wanted list already has an approved-or-seed entry
    dupes = conn.execute(
        "SELECT w.name FROM wanted_scientists w "
        "JOIN entries e ON e.scientist_name = w.name").fetchall()
    assert dupes == []


def test_reimport_is_idempotent(tmp_path):
    conn = db_mod.init_db(tmp_path / "seed.db", now=NOW)
    kw = dict(scientists_dir=SCIENTISTS, manifest_path=MANIFEST,
              photo_dir=tmp_path / "photos", now=NOW, decks_dir=None, fetch_photos=False)
    seed_import(conn, **kw)
    first = _count(conn, "SELECT count(*) FROM entries")
    seed_import(conn, **kw)  # run again
    assert _count(conn, "SELECT count(*) FROM entries") == first
    assert _count(conn, "SELECT count(*) FROM textbooks") == 3
    assert _count(conn, "SELECT count(*) FROM placements") == 65
    assert _count(conn, "SELECT count(*) FROM wanted_scientists") == 6  # not doubled


def test_reseed_preserves_live_textbook_links(tmp_path):
    """M2 review (critical): re-seeding must NOT null a class's textbook
    link. Textbooks are UPSERTed by slug, ids stay stable."""
    conn = db_mod.init_db(tmp_path / "seed.db", now=NOW)
    kw = dict(scientists_dir=SCIENTISTS, manifest_path=MANIFEST,
              photo_dir=tmp_path / "photos", now=NOW, decks_dir=None, fetch_photos=False)
    seed_import(conn, **kw)
    tb_id = conn.execute("SELECT id FROM textbooks WHERE slug='knight-calc-3rd'").fetchone()[0]
    # a teacher collection + a student placement referencing that textbook
    conn.execute(
        "INSERT INTO collections(slug,name,manage_token_hash,textbook_id,created_at) "
        "VALUES('cls','Class','hash',?,?)", (tb_id, NOW))
    conn.execute(
        "INSERT INTO entries(scientist_name,scientist_slug,created_at,updated_at) "
        "VALUES('S','s',?,?)", (NOW, NOW))
    sid = conn.execute("SELECT id FROM entries WHERE scientist_slug='s'").fetchone()[0]
    conn.execute(
        "INSERT INTO placements(entry_id,textbook_id,chapter,raw_line) VALUES(?,?,9,'x')",
        (sid, tb_id))
    conn.execute(
        "INSERT INTO wanted_scientists(name,is_seed) VALUES('User Pick',0)")
    conn.commit()

    seed_import(conn, **kw)  # re-seed

    assert conn.execute("SELECT id FROM textbooks WHERE slug='knight-calc-3rd'").fetchone()[0] == tb_id
    assert conn.execute("SELECT textbook_id FROM collections WHERE slug='cls'").fetchone()[0] == tb_id
    row = conn.execute("SELECT textbook_id, toc_row_id FROM placements WHERE entry_id=?", (sid,)).fetchone()
    assert row["textbook_id"] == tb_id and row["toc_row_id"] is not None  # re-linked
    # user-added wanted survives; seed wanted not doubled
    assert _count(conn, "SELECT count(*) FROM wanted_scientists WHERE is_seed=0") == 1


def test_reseed_refused_when_adopted_lineage_would_break(tmp_path):
    from nonnewtonian.importer import SeedRefused

    conn = db_mod.init_db(tmp_path / "seed.db", now=NOW)
    kw = dict(scientists_dir=SCIENTISTS, manifest_path=MANIFEST,
              photo_dir=tmp_path / "photos", now=NOW, decks_dir=None, fetch_photos=False)
    seed_import(conn, **kw)
    seed_id = conn.execute("SELECT id FROM entries WHERE seed_origin IS NOT NULL LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO entries(scientist_name,scientist_slug,adopted_from_entry_id,created_at,updated_at) "
        "VALUES('Adopted','adopted',?,?,?)", (seed_id, NOW, NOW))
    conn.commit()
    with pytest.raises(SeedRefused):
        seed_import(conn, **kw)
    seed_import(conn, force=True, **kw)  # force overrides


def test_chapter_not_in_toc_is_flagged(seeded):
    conn, _ = seeded
    # A placement whose chapter exceeds its textbook's TOC is flagged,
    # not silently counted as cleanly matched.
    flagged = conn.execute(
        "SELECT count(*) FROM placements WHERE flags LIKE '%chapter-not-in-toc%'"
    ).fetchone()[0]
    assert flagged >= 1
    # and such a placement keeps its textbook_id + raw line
    row = conn.execute(
        "SELECT textbook_id, raw_line FROM placements WHERE flags LIKE '%chapter-not-in-toc%' LIMIT 1"
    ).fetchone()
    assert row["textbook_id"] is not None and row["raw_line"].strip()


def test_dry_run_report_matches_real_counts(seeded):
    conn, report = seeded
    assert sum(f.entries_created for f in report.files) == _count(conn, "SELECT count(*) FROM entries")
    assert sum(f.placements_total for f in report.files) == 65
