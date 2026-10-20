"""M7 WP2: the `nonnewtonian migrate` subcommand and env-aware defaults."""

from nonnewtonian import cli
from nonnewtonian import db as db_mod


def test_migrate_applies_on_fresh_file_and_is_idempotent(tmp_path, capsys):
    db_path = tmp_path / "x.db"
    assert cli.main(["migrate", "--db", str(db_path), "--create"]) == 0
    first = capsys.readouterr().out
    assert first.startswith("applied: ")
    conn = db_mod.connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db_mod.pending_migrations(conn) == []
    conn.close()

    assert cli.main(["migrate", "--db", str(db_path)]) == 0
    assert capsys.readouterr().out.strip() == "up to date"


def test_migrate_check_exit_codes(tmp_path, capsys):
    db_path = tmp_path / "x.db"
    db_mod.connect(db_path).close()  # exists, nothing applied
    assert cli.main(["migrate", "--db", str(db_path), "--check"]) == 1
    names = [p.name for p in db_mod.migration_files()]
    assert capsys.readouterr().out.strip() == "pending: " + ", ".join(names)

    assert cli.main(["migrate", "--db", str(db_path)]) == 0
    capsys.readouterr()
    assert cli.main(["migrate", "--db", str(db_path), "--check"]) == 0
    assert capsys.readouterr().out.strip() == "up to date"


def test_migrate_check_does_not_create_missing_db(tmp_path, capsys):
    db_path = tmp_path / "missing.db"
    assert cli.main(["migrate", "--db", str(db_path), "--check"]) == 1
    assert "database missing" in capsys.readouterr().out
    assert not db_path.exists()


def test_migrate_without_migrations_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db_mod, "migration_files", lambda migrations_dir=None: [])
    db_path = tmp_path / "x.db"
    assert cli.main(["migrate", "--db", str(db_path)]) == 2
    assert "no migrations found under" in capsys.readouterr().out
    assert not db_path.exists()


def test_seed_import_defaults_honor_env(monkeypatch):
    monkeypatch.setenv("NNP_DB", "/srv/nnp/app.db")
    monkeypatch.setenv("NNP_PHOTOS", "/srv/nnp/photos")
    args = cli.build_parser().parse_args(["seed-import"])
    assert args.db == "/srv/nnp/app.db"
    assert args.photo_dir == "/srv/nnp/photos"


def test_seed_import_defaults_match_the_app(monkeypatch):
    monkeypatch.delenv("NNP_DB", raising=False)
    monkeypatch.delenv("NNP_PHOTOS", raising=False)
    args = cli.build_parser().parse_args(["seed-import"])
    assert args.db == "data/app.db"
    assert args.photo_dir == "data/photos"


def test_migrate_refuses_missing_db_without_create(tmp_path, capsys):
    # M7 review #32: a bare migrate used to create an empty DB with the
    # umask's mode (0640 under nnp-cli), which the preflight then rejects.
    db_path = tmp_path / "missing.db"
    assert cli.main(["migrate", "--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert "database missing" in out and "--create" in out and "install.sh" in out
    assert not db_path.exists()


def test_migrate_create_makes_a_0600_db_even_under_a_loose_umask(tmp_path):
    import os
    db_path = tmp_path / "new.db"
    old = os.umask(0o022)
    try:
        assert cli.main(["migrate", "--db", str(db_path), "--create"]) == 0
    finally:
        os.umask(old)
    assert db_path.stat().st_mode & 0o777 == 0o600
    conn = db_mod.connect(db_path)
    assert db_mod.pending_migrations(conn) == []
    conn.close()


def _mig_dir(tmp_path, files):
    d = tmp_path / "migs"
    d.mkdir()
    for name, sql in files.items():
        (d / name).write_text(sql)
    return d


def test_a_failing_migration_is_rolled_back_whole(tmp_path):
    # M7 review #36: executescript used to autocommit each statement, so a
    # failure halfway left a half-applied, unrecorded schema.
    import pytest
    import sqlite3
    d = _mig_dir(tmp_path, {
        "0001_a.sql": "CREATE TABLE a (x);",
        "0002_b.sql": "CREATE TABLE b (x);\nINSERT INTO a VALUES (1);\nCREATE TABLE a (y);",
    })
    conn = db_mod.connect(tmp_path / "m.db")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        db_mod.migrate(conn, now="t", migrations_dir=d)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables and "b" not in tables
    assert conn.execute("SELECT count(*) FROM a").fetchone()[0] == 0
    assert db_mod.applied_migrations(conn) == {"0001_a.sql"}
    assert not conn.in_transaction
    # fix the file and rerun: it applies cleanly
    (d / "0002_b.sql").write_text("CREATE TABLE b (x);\nINSERT INTO a VALUES (1);")
    assert db_mod.migrate(conn, now="t", migrations_dir=d) == ["0002_b.sql"]
    conn.close()


def test_migration_with_its_own_transaction_control_is_refused(tmp_path):
    import pytest
    d = _mig_dir(tmp_path, {"0001_a.sql": "BEGIN;\nCREATE TABLE a (x);\nCOMMIT;"})
    conn = db_mod.connect(tmp_path / "m.db")
    with pytest.raises(RuntimeError, match="own transaction control"):
        db_mod.migrate(conn, now="t", migrations_dir=d)
    assert db_mod.applied_migrations(conn) == set()
    conn.close()


def test_shipped_migrations_carry_no_transaction_control():
    for path in db_mod.migration_files():
        assert not db_mod._TXN_STMT.search(path.read_text()), path.name


def test_trigger_body_is_not_mistaken_for_transaction_control(tmp_path):
    d = _mig_dir(tmp_path, {"0001_a.sql": (
        "CREATE TABLE a (x);\nCREATE TABLE log (x);\n"
        "CREATE TRIGGER t AFTER INSERT ON a\nBEGIN\n  INSERT INTO log VALUES (new.x);\nEND;")})
    conn = db_mod.connect(tmp_path / "m.db")
    assert db_mod.migrate(conn, now="t", migrations_dir=d) == ["0001_a.sql"]
    conn.close()
