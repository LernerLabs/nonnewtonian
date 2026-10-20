# Encrypted off-site backups (M7)

This makes the privacy-page line "encrypted backups kept off-site" actually
true. It backs up a consistent SQLite snapshot + the photos directory to a
[restic](https://restic.net) repository on Backblaze B2 (Backblaze's object
storage service), nightly, via a systemd timer, and gives you a one-command
restore test. Each run pings a healthchecks.io dead-man check (a check that
alerts when an expected ping does NOT arrive), so a backup that silently stops
running still reaches your inbox.

**Every command below is for you to run on the server.** Claude wrote these
files but does not and will not connect to your server or run anything on it.

The one real footgun this avoids: the live `app.db` is in WAL (write-ahead log)
mode, so a plain file copy can grab a torn, half-written database.
`nnp-backup.sh` takes a consistent snapshot with sqlite's `.backup` API and
integrity-checks it, so a bad copy fails loudly instead of silently.

The plaintext snapshot is staged in the root-owned `/var/lib/nnp-backup/staging`
(mode 0700), never inside the nnp-owned `/var/lib/nnp`, where the app user
could plant a symlink for root to overwrite.

## Files here

| file                     | goes to                                                |
| ------------------------ | ------------------------------------------------------ |
| `nnp-backup.sh`          | stays in the repo checkout (`/opt/nnp/deploy/backup/`) |
| `nnp-restore-test.sh`    | same                                                   |
| `nnp-backup.env.example` | copy to `/etc/nnp-backup.env`                          |
| `nnp-backup.service`     | `/etc/systemd/system/` (install.sh copies it)          |
| `nnp-backup.timer`       | `/etc/systemd/system/` (install.sh copies it)          |

## One-time setup (on the server)

1. **Run the main install first** (it installs restic and sqlite3 and copies
   the backup units): `sudo /opt/nnp/deploy/install.sh`, per
   `deploy/RUNBOOK.md`.

2. **Create the off-site target.** In the Backblaze web console: make a
   *private* B2 bucket, then an application key scoped to it. Note the bucket
   name, the `keyID`, and the `applicationKey`.

3. **Create the dead-man check.** At healthchecks.io, add a check named
   `nnp-backup` with period 1 day and grace 3 hours, and copy its ping URL
   (`https://hc-ping.com/...`). **Treat the ping URL as a secret**: anyone
   holding it can send fake "success" pings and hide a dead backup. Keep it in
   your password manager and in the root-only env file, nowhere else.

4. **Pick the encryption passphrase and save it in your password manager first**
   (it is the only key to the backups). Then put it on the server root-only.
   The directory and file are created 0700/0600 from the start (never briefly
   world-readable), and the passphrase is pasted at a hidden prompt, so it
   never appears on a command line or in your shell history:
   ```
   sudo install -d -m 700 /etc/nnp-backup
   sudo bash -c 'umask 077; install -m 600 /dev/null /etc/nnp-backup/restic-pass; IFS= read -rsp "passphrase: " p; echo; printf "%s" "$p" > /etc/nnp-backup/restic-pass'
   sudo ls -l /etc/nnp-backup/restic-pass
   ```
   You should see: `-rw-------` and `root root` on the file.

5. **Write the config** (chmod BEFORE you type secrets into it):
   ```
   sudo cp /opt/nnp/deploy/backup/nnp-backup.env.example /etc/nnp-backup.env
   sudo chmod 600 /etc/nnp-backup.env
   sudo nano /etc/nnp-backup.env        # bucket, B2 keys, NNP_BACKUP_PING_URL
   ```

6. **Initialise, back up, test the restore, and enable the timer** in one go:
   ```
   sudo /opt/nnp/deploy/install.sh --enable-backups
   ```
   It refuses to run while the env file still has `YOUR_BUCKET_NAME` or the
   `YOUR-UUID` placeholder, or has no `NNP_BACKUP_PING_URL`. It runs
   `restic init` only if the repository does not exist yet, then one backup,
   then `nnp-restore-test.sh`, then enables `nnp-backup.timer`.
   You should see: `snapshot ... saved`, `nnp backup complete`,
   `restore OK: integrity ok, N entries in the restored database`, the timer
   in the `list-timers` output, and the healthchecks.io check turn green.

**Only after step 6 passes is the privacy page's "encrypted backups kept
off-site" line true.** Until then, don't publish that page (or soften the line).

The restore test restores into a temp dir, so it needs free disk at least the
size of the backup (database + photos). Rerun it occasionally:

```
sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; exec /opt/nnp/deploy/backup/nnp-restore-test.sh'
```

To read the last run: `journalctl -u nnp-backup.service -n 40 --no-pager`.

## Restoring for real

Stop the app, then restore the snapshot you want into a scratch dir:

```
sudo systemctl stop nnp
sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; restic snapshots'
sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; restic restore latest --target /root/restore'
sudo find /root/restore -name app.db
```

You should see: one path ending in `app.db` (normally
`/root/restore/var/lib/nnp-backup/staging/app.db`).

**Move the old `app.db`, `app.db-wal` and `app.db-shm` aside before copying
the restored file in.** A stale `-wal` left next to a restored `app.db` is
REPLAYED into it on the next open and corrupts it.

```
sudo mkdir -p /root/pre-restore
sudo sh -c 'mv /var/lib/nnp/app.db* /root/pre-restore/'
sudo mv /var/lib/nnp/photos /root/pre-restore/photos
sudo cp /root/restore/var/lib/nnp-backup/staging/app.db /var/lib/nnp/app.db
sudo cp -a /root/restore/var/lib/nnp/photos /var/lib/nnp/photos
sudo chown -R nnp:nnp /var/lib/nnp
sudo chmod 600 /var/lib/nnp/app.db
sudo systemctl start nnp
curl -fsS http://127.0.0.1:8000/healthz
```

You should see: `ok`. (Use the path `find` printed if it differs from the one
in the `cp` line.)

## Retention across a rebuild

Every snapshot is recorded with the fixed host name `nnp` (`restic backup
--host nnp`), and the nightly `restic forget` groups snapshots by tag only
(`--group-by tags`). A rebuilt server with a different hostname, or a changed
`NNP_BACKUP_STAGING`, therefore keeps pruning the old snapshots, and nothing
outlives the six-month window. Snapshots taken before this change carry the
real hostname but the same `nnp` tag, so they are pruned with the rest. To
check, list them:

```
sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; restic snapshots'
```

You should see: the oldest snapshot no more than about six months old, apart
from any tagged `pre-migration` (next section).

## Pre-migration snapshots (prune by hand)

When a deploy has migrations to run, `install.sh` first takes a backup tagged
`nnp,pre-migration` (via `NNP_BACKUP_TAG=pre-migration`), the snapshot its
rollback hint tells you to restore. The nightly `forget` keeps every snapshot
tagged `pre-migration` (`--keep-tag pre-migration`), so the nightly run cannot
prune it before you roll back. **That means nothing prunes them**: each one
outlives the six-month window the privacy page promises until you remove it.
Once the deploy has proved good (and at least every few months), list them and
forget the ones you no longer need:

```
sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; restic snapshots --tag pre-migration'
sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; restic forget --prune SNAPSHOT_ID'
```

You should see: the first command lists each pre-migration snapshot with its
ID and date; the second prints `removed snapshot ...` and then the prune
summary. Never forget the newest one while a rollback is still possible.

Rebuilding on a new instance, the laptop restore drill, and the rest of the
operations are in `deploy/RUNBOOK.md`.
