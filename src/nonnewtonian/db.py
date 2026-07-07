"""SQLite access: connection setup and a tiny migration runner.

No ORM.  Migrations are numbered ``NNNN_name.sql`` files in the
``migrations/`` directory, applied in order and recorded in
``applied_migrations`` so re-running is idempotent.

Concurrency posture (from the plan's adversarial review): WAL journal
mode so readers never block the writer, a busy timeout so the two
gunicorn workers wait rather than erroring on lock contention, and
foreign keys on.  ``connect`` applies these per connection; the web app
opens one connection per request.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000

# migrations/ lives at the repo root, two levels up from this file's package.
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def connect(db_path) -> sqlite3.Connection:
    """Open a tuned connection (WAL, busy timeout, FKs, Row factory)."""
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS applied_migrations ("
        " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def applied_migrations(conn: sqlite3.Connection) -> set[str]:
    _ensure_migrations_table(conn)
    return {row["name"] for row in conn.execute("SELECT name FROM applied_migrations")}


def migration_files(migrations_dir: Path | None = None) -> list[Path]:
    directory = migrations_dir or _MIGRATIONS_DIR
    return sorted(directory.glob("[0-9]*.sql"))


def migrate(conn: sqlite3.Connection, *, now: str, migrations_dir: Path | None = None) -> list[str]:
    """Apply pending migrations in order.  Returns the names applied.

    ``now`` is passed in (never generated here) so callers control the
    timestamp and runs are reproducible.
    """
    done = applied_migrations(conn)
    newly: list[str] = []
    for path in migration_files(migrations_dir):
        if path.name in done:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO applied_migrations(name, applied_at) VALUES(?, ?)",
            (path.name, now),
        )
        conn.commit()
        newly.append(path.name)
    return newly


def assert_wal(conn: sqlite3.Connection) -> None:
    """Startup preflight: refuse to run on a non-WAL database."""
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "wal":
        raise RuntimeError(
            f"database journal_mode is {mode!r}, expected 'wal' — "
            "refusing to start (concurrent writes would corrupt or 500)."
        )


def init_db(db_path, *, now: str) -> sqlite3.Connection:
    """Open a connection and bring the schema up to date."""
    conn = connect(db_path)
    migrate(conn, now=now)
    return conn
