from nonnewtonian import db as db_mod

NOW = "2026-07-07T00:00:00+00:00"


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    conn = db_mod.connect(tmp_path / "t.db")
    applied = db_mod.migrate(conn, now=NOW)
    assert applied == ["0001_initial.sql"]
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("textbooks", "toc_rows", "collections", "entries",
                     "placements", "photos", "wanted_scientists"):
        assert expected in tables
    # second run applies nothing
    assert db_mod.migrate(conn, now=NOW) == []


def test_wal_enabled_and_asserted(tmp_path):
    conn = db_mod.connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    db_mod.assert_wal(conn)  # does not raise


def test_foreign_keys_enforced(tmp_path):
    import sqlite3
    import pytest

    conn = db_mod.init_db(tmp_path / "t.db", now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO placements(entry_id, raw_line) VALUES(9999, 'x')"
        )
        conn.commit()
