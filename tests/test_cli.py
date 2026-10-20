"""M7 WP2: the `nonnewtonian migrate` subcommand and env-aware defaults."""

from nonnewtonian import cli
from nonnewtonian import db as db_mod


def test_migrate_applies_on_fresh_file_and_is_idempotent(tmp_path, capsys):
    db_path = tmp_path / "x.db"
    assert cli.main(["migrate", "--db", str(db_path)]) == 0
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
