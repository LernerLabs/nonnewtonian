"""Behavior of deploy/backup/nnp-backup.sh, run against stub restic, sqlite3
and curl executables that record their argv. Nothing real is backed up or
pinged; the real tools only run on the server."""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKUP_SH = ROOT / "deploy/backup/nnp-backup.sh"
PING = "https://hc-ping.example/uuid"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUBS = {
    "restic": """#!/bin/sh
echo "restic $*" >> "$STUB_LOG"
if [ "$1" = backup ] && [ -n "${RESTIC_BACKUP_RC:-}" ]; then exit "$RESTIC_BACKUP_RC"; fi
exit 0
""",
    # `.backup 'PATH'` creates PATH; the integrity check prints ok.
    "sqlite3": """#!/bin/sh
echo "sqlite3 $*" >> "$STUB_LOG"
case "$2" in
  .backup*) path=$(printf '%s' "$2" | sed "s/^\\.backup '\\(.*\\)'$/\\1/"); : > "$path" ;;
  PRAGMA*) echo ok ;;
esac
""",
    "curl": """#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
""",
}


def _run(tmp_path, *, drop=(), **extra):
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        p = stubs / name
        p.write_text(body)
        p.chmod(0o755)
    (tmp_path / "app.db").touch()
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "NNP_DB": str(tmp_path / "app.db"),
        "NNP_PHOTOS": str(tmp_path / "photos"),
        "RESTIC_REPOSITORY": "b2:test:nnp",
        "NNP_BACKUP_PING_URL": PING,
        "NNP_BACKUP_STAGING": str(tmp_path / "staging"),
    }
    env.update(extra)
    for key in drop:
        env.pop(key)
    result = subprocess.run(["bash", str(BACKUP_SH)], env=env, capture_output=True, text=True)
    return result, log.read_text().splitlines()


def _pings(calls):
    return [c.split()[-1] for c in calls if c.startswith("curl ")]


def test_success_pings_start_then_success_and_removes_snapshot(tmp_path):
    result, calls = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert _pings(calls) == [PING + "/start", PING]
    assert not (tmp_path / "staging/app.db").exists()
    assert oct((tmp_path / "staging").stat().st_mode & 0o777) == "0o700"


def test_unlock_runs_before_backup(tmp_path):
    _, calls = _run(tmp_path)
    restic = [c.split()[1] for c in calls if c.startswith("restic ")]
    assert restic.index("unlock") < restic.index("backup")


def test_failed_restic_backup_pings_fail_and_exits_nonzero(tmp_path):
    result, calls = _run(tmp_path, RESTIC_BACKUP_RC="1")
    assert result.returncode != 0
    assert _pings(calls) == [PING + "/start", PING + "/fail"]
    assert not (tmp_path / "staging/app.db").exists()


def test_missing_ping_url_refuses_before_touching_the_db(tmp_path):
    result, calls = _run(tmp_path, drop=("NNP_BACKUP_PING_URL",))
    assert result.returncode != 0
    assert "NNP_BACKUP_PING_URL" in result.stderr
    assert calls == []


def test_staging_inside_the_nnp_tree_is_refused(tmp_path):
    result, calls = _run(tmp_path, NNP_BACKUP_STAGING="/var/lib/nnp/backup-staging")
    assert result.returncode != 0
    assert not any(c.startswith("sqlite3 ") for c in calls)


def test_missing_db_pings_fail_without_running_sqlite(tmp_path):
    result, calls = _run(tmp_path, NNP_DB=str(tmp_path / "no-such.db"))
    assert result.returncode != 0
    assert _pings(calls) == [PING + "/start", PING + "/fail"]
    assert not any(c.startswith("sqlite3 ") for c in calls)


def test_retention_does_not_depend_on_hostname_or_paths(tmp_path):
    # restic's default forget grouping is host+paths: a rebuilt server would
    # leave the old host's snapshots unpruned forever (review #9).
    _, calls = _run(tmp_path)
    backup = next(c for c in calls if c.startswith("restic backup "))
    forget = next(c for c in calls if c.startswith("restic forget "))
    assert "--host nnp" in backup
    assert "--group-by tags" in forget and "--tag nnp" in forget
