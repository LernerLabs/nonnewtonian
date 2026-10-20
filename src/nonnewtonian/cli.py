"""Command-line entry points for NonNewtonian Physicists.

Until the Flask app lands (M3+), the seed importer runs via::

    python -m nonnewtonian.cli seed-import --db app.db [--dry-run] [--no-photos]

Timestamps are passed in explicitly (the library never calls a clock
itself), so the CLI stamps ``--now`` once here.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

from . import db as db_mod
from .importer import seed_import

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SCIENTISTS = _REPO_ROOT / "tests" / "fixtures" / "scientists"
_DEFAULT_TEXTBOOKS = _REPO_ROOT / "data" / "textbooks"


def _default_decks_dir() -> str | None:
    """Original .pptx decks for dead-photo recovery.  From $NNP_DECKS, or
    a sibling IntroductoryPhysics checkout if present; else none."""
    env = os.environ.get("NNP_DECKS")
    if env:
        return env
    sibling = _REPO_ROOT.parent / "IntroductoryPhysics" / "Textbooks"
    return str(sibling) if sibling.exists() else None


def _print_report(report) -> None:
    print("=== Seed report ===")
    for line in report.summary_lines():
        print("  " + line)
    print("--- per file ---")
    for f in report.files:
        bits = [f"{f.filename}: {f.name}"]
        bits.append(f"entries={f.entries_created}")
        bits.append(
            f"placements={f.placements_total}"
            f"(m{f.placements_matched}/u{f.placements_unassigned})"
        )
        bits.append(
            f"photos={f.photos_total}"
            f"(s{f.photos_stored}/r{f.photos_recovered}/d{f.photos_dead})"
        )
        if f.fields_missing:
            bits.append("missing=" + ",".join(f.fields_missing))
        if f.corrections:
            bits.append("corrected=" + "; ".join(f.corrections))
        if f.flags:
            bits.append("flags=" + "; ".join(f.flags))
        print("  " + " ".join(bits))


def cmd_seed_import(args: argparse.Namespace) -> int:
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    photo_dir = Path(args.photo_dir)
    decks_dir = Path(args.decks_dir) if args.decks_dir else None
    if decks_dir and not decks_dir.exists():
        decks_dir = None

    if args.dry_run:
        # Dry run: temp db, no photo fetching, write nothing durable.
        with tempfile.TemporaryDirectory() as tmp:
            conn = db_mod.init_db(Path(tmp) / "dry.db", now=now)
            report = seed_import(
                conn, scientists_dir=Path(args.scientists), textbooks_dir=Path(args.textbooks),
                photo_dir=Path(tmp) / "photos", now=now, decks_dir=decks_dir,
                fetch_photos=False,
            )
            conn.close()
        _print_report(report)
        print("\n(dry run — no database written)")
        return 0

    Path(args.db).resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = db_mod.init_db(args.db, now=now)
    db_mod.assert_wal(conn)
    report = seed_import(
        conn, scientists_dir=Path(args.scientists), textbooks_dir=Path(args.textbooks),
        photo_dir=photo_dir, now=now, decks_dir=decks_dir,
        fetch_photos=not args.no_photos,
    )
    conn.close()
    _print_report(report)
    print(f"\nWrote {args.db}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    if not db_mod.migration_files():
        print(f"no migrations found under {db_mod._MIGRATIONS_DIR}; "
              "install editable (pip install -e)")
        return 2
    if args.check:
        # Never create the file here: the production preflight relies on a
        # missing DB staying missing (sqlite3.connect would make an empty one).
        if not Path(args.db).exists():
            print(f"database missing at {args.db}")
            return 1
        conn = db_mod.connect(args.db)
        pending = db_mod.pending_migrations(conn)
        conn.close()
        if pending:
            print("pending: " + ", ".join(pending))
            return 1
        print("up to date")
        return 0
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    conn = db_mod.connect(args.db)
    applied = db_mod.migrate(conn, now=now)
    db_mod.assert_wal(conn)
    conn.close()
    print("applied: " + ", ".join(applied) if applied else "up to date")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nonnewtonian")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed-import", help="import the original corpus as communal-pending seeds")
    # Same defaults as the web app (web/__init__.py), so a bare command
    # writes where the app reads.
    seed.add_argument("--db", default=os.environ.get("NNP_DB", "data/app.db"),
                      help="SQLite database path ($NNP_DB)")
    seed.add_argument("--scientists", default=str(_DEFAULT_SCIENTISTS))
    seed.add_argument("--textbooks", default=str(_DEFAULT_TEXTBOOKS),
                      help="dir of per-textbook <slug>.toml files")
    seed.add_argument("--photo-dir", default=os.environ.get("NNP_PHOTOS", "data/photos"),
                      help="content-hash photo tree ($NNP_PHOTOS)")
    seed.add_argument("--decks-dir", default=_default_decks_dir(),
                      help="original .pptx decks for dead-photo recovery ($NNP_DECKS)")
    seed.add_argument("--dry-run", action="store_true")
    seed.add_argument("--no-photos", action="store_true", help="skip network photo fetching")
    seed.set_defaults(func=cmd_seed_import)

    mig = sub.add_parser("migrate", help="apply pending schema migrations")
    mig.add_argument("--db", required=True, help="SQLite database path")
    mig.add_argument("--check", action="store_true",
                     help="only list pending migrations; exit 1 if any")
    mig.set_defaults(func=cmd_migrate)
    # "export-static" is reserved for the static-export subcommand (M7 WP10).
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
