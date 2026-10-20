#!/usr/bin/env bash
# Nightly encrypted off-site backup for NonNewtonian Physicists: a CONSISTENT
# SQLite snapshot + the photos dir, pushed to a restic repo (Backblaze B2 by
# default). Run by nnp-backup.timer; all config comes from the systemd
# EnvironmentFile (see nnp-backup.env.example).
#
# This script is meant to run ON THE SERVER, by you. It is committed here as
# deploy tooling; nothing in this repo connects to your server for you.
set -euo pipefail
umask 077

: "${NNP_DB:?set NNP_DB to the sqlite database path}"
: "${NNP_PHOTOS:?set NNP_PHOTOS to the photos directory}"
: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY (e.g. b2:bucket:nnp)}"
: "${NNP_BACKUP_PING_URL:?set NNP_BACKUP_PING_URL (healthchecks.io): an unmonitored backup can stop silently}"
# restic reads RESTIC_PASSWORD / RESTIC_PASSWORD_FILE and the B2_* creds from
# the environment; systemd's EnvironmentFile supplies them.

# Dead-man pings: /start, then "" on success or /fail on any error exit. A
# failed ping never fails the backup; a MISSING success ping is what alerts.
ping() { curl -fsS -m 10 --retry 3 -o /dev/null "${NNP_BACKUP_PING_URL}$1" || true; }

# Root-owned: never inside the nnp-owned tree, where the app user could plant
# a symlink at the snapshot path and have root overwrite any file.
staging="${NNP_BACKUP_STAGING:-/var/lib/nnp-backup/staging}"
case "$staging" in
    /var/lib/nnp/*) echo "NNP_BACKUP_STAGING=$staging is inside the nnp-owned tree; use /var/lib/nnp-backup/staging" >&2; exit 1 ;;
esac
mkdir -p "$staging"
chmod 700 "$staging"
snap="$staging/app.db"
# A STABLE staging path (not mktemp) so the backed-up path is predictable for
# restore. The plaintext copy is removed on exit.
trap 'rc=$?; rm -f "$snap"; [ "$rc" -eq 0 ] || ping /fail' EXIT
ping /start
# sqlite3 would silently CREATE an empty database (as root) at a wrong path.
[ -f "$NNP_DB" ] || { echo "no database at NNP_DB=$NNP_DB" >&2; exit 1; }

# Consistent snapshot: sqlite's online-backup API copies a coherent image even
# while the app is mid-write in WAL mode. NEVER back up the live app.db by file
# copy -- you can capture a torn, half-written page and not know until restore.
# sqlite3 runs as root; SQLite chowns any -wal/-shm it creates to the DB owner (nnp).
sqlite3 "$NNP_DB" ".backup '$snap'"

# Fail loudly if the snapshot itself is already corrupt -- don't ship a bad backup.
integ="$(sqlite3 "$snap" 'PRAGMA integrity_check;')"
[ "$integ" = "ok" ] || { echo "snapshot integrity_check failed: $integ" >&2; exit 1; }

# Clears only STALE locks (left by an OOM kill or reboot mid-backup);
# otherwise every later night fails on the leftover lock.
restic unlock

# A fixed --host, and forget grouping by tag alone (restic's default groups by
# host+paths): a rebuilt server with a new hostname, or a changed staging path,
# must still prune the old snapshots, or removed content outlives the six-month
# retention the privacy page promises. Snapshots taken before this fix carry
# the real hostname; they share the "nnp" tag group, so they are pruned too.
restic backup --host nnp --tag nnp "$snap" "$NNP_PHOTOS"

restic forget --group-by tags --tag nnp \
  --keep-daily "${KEEP_DAILY:-7}" \
  --keep-weekly "${KEEP_WEEKLY:-4}" \
  --keep-monthly "${KEEP_MONTHLY:-6}" \
  --prune

ping ""
echo "nnp backup complete: $(date -u +%FT%TZ)"
