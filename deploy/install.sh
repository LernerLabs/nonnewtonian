#!/usr/bin/env bash
# Install AND update NonNewtonian Physicists on the server. Idempotent: rerun
# it after every `git pull`. Run by you, on the server, as root:
#
#   first time:  sudo git clone https://github.com/mglerner/nonnewtonian /opt/nnp
#                sudo /opt/nnp/deploy/install.sh
#   update:      sudo git -C /opt/nnp pull --ff-only && sudo /opt/nnp/deploy/install.sh
#   backups:     sudo /opt/nnp/deploy/install.sh --enable-backups
#   mirror:      sudo /opt/nnp/deploy/install.sh --mirror-key
#                (add the printed key to GitHub, write /etc/nnp/mirror.env)
#                sudo /opt/nnp/deploy/install.sh --enable-mirror
#   self-check:  sudo /opt/nnp/deploy/install.sh --enable-selfcheck
#
# Every step prints "== step" and a "You should see:" line; read them.
# Nothing in this repo connects to the server for you.
#
# NOT verified where this was written (no Ubuntu box, no root, no network):
# the apt/Caddy repo commands, `caddy validate`, and the systemd units. The
# first real run on the server is the test; read each "You should see:" line.
set -euo pipefail

REPO=/opt/nnp
ENV_FILE=/etc/nnp/nnp.env
DB=/var/lib/nnp/app.db
SITE=https://nonnewtonian.org
LAST_GOOD=/etc/nnp/last-good-commit
export DEBIAN_FRONTEND=noninteractive

step() { echo; echo "== $*"; }
see() { echo "You should see: $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
# nnp owns /var/lib/nnp, so a compromised app could plant a link there for
# root to follow; refuse instead.
no_symlink() { [ ! -L "$1" ] || die "$1 is a symlink; the app user may be compromised: investigate before rerunning"; }
# Read one variable out of a root-only env file without running it: systemd
# reads nnp.env too, and it takes `KEY=a b $c` literally where a shell would
# not. Matches systemd for plain KEY=value lines and for one pair of quotes
# around the whole value; NOT for backslash escapes, line continuations or
# leading whitespace (nnp.env.example says to avoid those). Same parser as
# deploy/nnp-cli. The last assignment wins, as in systemd.
envget() {
    local line val=
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in "$2="*) val=${line#*=} ;; esac
    done < "$1"
    case "$val" in
        \'*\') val=${val#\'}; val=${val%\'} ;;
        \"*\") val=${val#\"}; val=${val%\"} ;;
    esac
    printf '%s' "$val"
}

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 $*"
[ "$(readlink -f "$0")" = "$REPO/deploy/install.sh" ] \
    || die "install.sh must run from $REPO/deploy/install.sh (clone the repo to $REPO)"

# Hand the DB files to nnp without following links: explicit names (no glob),
# chown -h, and chmod (which always follows) run as nnp, who owns the file by
# then, so a link swapped in after the check gains nothing.
own_db_files() {
    local f
    for f in "$DB" "$DB-wal" "$DB-shm"; do
        no_symlink "$f"
        if [ -e "$f" ]; then chown -h nnp:nnp "$f"; fi
    done
    runuser -u nnp -- chmod 600 "$DB"
}

# Before a migration: snapshot with an extra restic tag, run directly rather
# than through nnp-backup.service so the tag can be passed. nnp-backup.sh
# keeps pre-migration snapshots out of its pruning (prune them by hand).
# Sets SNAPSHOT=1 only when the snapshot was taken.
premigration_backup() {
    if systemctl is-enabled --quiet nnp-backup.timer; then
        echo "pending migrations: taking a pre-migration backup snapshot first"
        bash -c "set -a; . /etc/nnp-backup.env; set +a; NNP_BACKUP_TAG=pre-migration exec /opt/nnp/deploy/backup/nnp-backup.sh" \
            || die "pre-migration backup failed; not migrating (output above)"
        SNAPSHOT=1
    else
        echo "WARN: pending migrations and backups are not enabled yet; no pre-migration snapshot"
    fi
}

# Only on the run that created the env file (step 5 and again at the end).
print_admin_url_once() {
    [ "${FRESH:-0}" = 1 ] || return 0
    echo
    echo "SAVE THIS IN YOUR PASSWORD MANAGER NOW: $SITE/admin/$(envget "$ENV_FILE" NNP_ADMIN_TOKEN)"
    echo "Also save NNP_SECRET_KEY from: sudo cat $ENV_FILE"
}

enable_backups() {
    local benv=/etc/nnp-backup.env
    step "enable backups"
    [ -e "$benv" ] || die "$benv missing: copy deploy/backup/nnp-backup.env.example there and edit it (deploy/backup/README.md step 5)"
    [ -e "$DB" ] || die "no database at $DB yet; seed and upload it first (RUNBOOK 1.5-1.6)"
    ! grep -q YOUR_BUCKET_NAME "$benv" || die "$benv still contains YOUR_BUCKET_NAME; set the real bucket"
    grep -q '^NNP_BACKUP_PING_URL=..*' "$benv" || die "$benv lacks NNP_BACKUP_PING_URL (healthchecks.io); backups refuse to run without it"
    ! grep -q YOUR-UUID "$benv" || die "$benv still contains the YOUR-UUID placeholder in NNP_BACKUP_PING_URL"
    [ -e /etc/systemd/system/nnp-backup.service ] || die "backup units not installed; run sudo $REPO/deploy/install.sh first"
    chown root:root "$benv"; chmod 600 "$benv"
    # restic init only when the repository does not exist yet (re-runs and
    # rebuilds on a new instance reuse it).
    bash -c "set -a; . $benv; set +a; restic cat config >/dev/null 2>&1 || restic init"
    systemctl start nnp-backup.service || { journalctl -u nnp-backup.service -n 40 --no-pager; die "first backup failed (log above)"; }
    journalctl -u nnp-backup.service -n 15 --no-pager
    bash -c "set -a; . $benv; set +a; exec $REPO/deploy/backup/nnp-restore-test.sh"
    systemctl enable --now nnp-backup.timer
    systemctl list-timers nnp-backup.timer --no-pager
    see "'snapshot ... saved' and 'nnp backup complete' in the log, 'restore OK: integrity ok, N entries', the timer listed, and the healthchecks.io check green"
}

mirror_key() {
    local d=/var/lib/nnp/.ssh
    step "mirror deploy key"
    id nnp >/dev/null 2>&1 || die "user nnp missing; run sudo $REPO/deploy/install.sh first"
    no_symlink "$d"
    install -d -o nnp -g nnp -m 700 "$d"
    # Everything inside .ssh runs as nnp: nnp owns the directory, so root
    # writing, chowning or reading there would follow links nnp planted.
    [ -e "$d/mirror_ed25519" ] \
        || runuser -u nnp -- ssh-keygen -q -t ed25519 -N '' -C nnp-mirror@nonnewtonian.org -f "$d/mirror_ed25519"
    # Recorded once (trust on first use); delete known_hosts to re-record.
    runuser -u nnp -- sh -c 'umask 022; [ -s "$1/known_hosts" ] && exit 0
        ssh-keyscan -t ed25519 github.com > "$1/known_hosts.new" 2>/dev/null
        [ -s "$1/known_hosts.new" ] || exit 1
        mv "$1/known_hosts.new" "$1/known_hosts"' sh "$d" \
        || die "ssh-keyscan github.com returned nothing (network?)"
    echo "Deploy key (GitHub: mglerner/nonnewtonian-mirror -> Settings -> Deploy keys -> Add, WITH write access):"
    runuser -u nnp -- cat "$d/mirror_ed25519.pub"
    echo "github.com host key recorded in $d/known_hosts:"
    runuser -u nnp -- ssh-keygen -lf "$d/known_hosts"
    echo "Compare that SHA256 with https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints"
    see "an ssh-ed25519 public key line, and a github.com ED25519 SHA256 fingerprint that matches GitHub's page exactly (if not, STOP: delete $d/known_hosts)"
}

enable_mirror() {
    local menv=/etc/nnp/mirror.env
    step "enable mirror"
    [ -e "$menv" ] || die "$menv missing: copy deploy/mirror/mirror.env.example there and edit it (RUNBOOK, mirror)"
    [ -e /var/lib/nnp/.ssh/mirror_ed25519 ] || die "no deploy key yet; run sudo $0 --mirror-key first"
    [ -e /etc/systemd/system/nnp-mirror.service ] || die "mirror units not installed; run sudo $REPO/deploy/install.sh first"
    chown root:root "$menv"; chmod 600 "$menv"
    systemctl start nnp-mirror.service || { journalctl -u nnp-mirror.service -n 40 --no-pager; die "first mirror run failed (log above)"; }
    journalctl -u nnp-mirror.service -n 15 --no-pager
    systemctl enable --now nnp-mirror.timer
    systemctl list-timers nnp-mirror.timer --no-pager
    see "'pushed <sha> (tree <sha>)' (or 'mirror unchanged') in the log and the timer listed; then turn on GitHub Pages for branch gh-pages"
}

enable_selfcheck() {
    local menv=/etc/nnp/monitor.env
    step "enable daily self-check"
    [ -e "$menv" ] || die "$menv missing: copy deploy/monitor/monitor.env.example there and set NNP_SELFCHECK_PING_URL"
    grep -q '^NNP_SELFCHECK_PING_URL=..*' "$menv" || die "$menv lacks NNP_SELFCHECK_PING_URL (healthchecks.io)"
    ! grep -q YOUR-UUID "$menv" || die "$menv still contains the YOUR-UUID placeholder in NNP_SELFCHECK_PING_URL"
    [ -e /etc/systemd/system/nnp-selfcheck.service ] || die "self-check units not installed; run sudo $REPO/deploy/install.sh first"
    chown root:root "$menv"; chmod 600 "$menv"
    systemctl start nnp-selfcheck.service || { journalctl -u nnp-selfcheck.service -n 20 --no-pager; die "self-check found problems (log above); fix them and rerun"; }
    journalctl -u nnp-selfcheck.service -n 5 --no-pager
    systemctl enable --now nnp-selfcheck.timer
    systemctl list-timers nnp-selfcheck.timer --no-pager
    see "'selfcheck ok' in the log, the timer listed, and the healthchecks.io check green"
}

case "${1:-}" in
    "") ;;
    --enable-backups) enable_backups; exit 0 ;;
    --mirror-key) mirror_key; exit 0 ;;
    --enable-mirror) enable_mirror; exit 0 ;;
    --enable-selfcheck) enable_selfcheck; exit 0 ;;
    *) die "usage: $0 [--enable-backups | --mirror-key | --enable-mirror | --enable-selfcheck]" ;;
esac

FRESH=0

step "1/9 swap"
if [ -z "$(swapon --show --noheadings)" ]; then
    if [ ! -f /swapfile ]; then
        fallocate -l 1G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
    fi
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/99-nnp.conf
sysctl -q -p /etc/sysctl.d/99-nnp.conf
swapon --show
sysctl vm.swappiness
see "a swap line (e.g. /swapfile file 1024M) and vm.swappiness = 10"

step "2/9 packages, unattended upgrades, journald"
apt-get update -q
apt-get install -y -q python3-venv git sqlite3 restic curl unattended-upgrades
if ! apt-cache policy caddy | grep -q dl.cloudsmith.io; then
    # Official Caddy apt repo, from https://caddyserver.com/docs/install
    # ("Debian, Ubuntu, Raspbian" -> stable). Written from memory where no
    # network was available: CHECK THESE AGAINST THAT PAGE before the first run.
    apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl
    rm -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    chmod o+r /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
fi
apt-get install -y -q caddy
caddy version
install -m 644 "$REPO/deploy/system/52nnp-unattended-upgrades" /etc/apt/apt.conf.d/52nnp-unattended-upgrades
install -d -m 755 /etc/systemd/journald.conf.d
install -m 644 "$REPO/deploy/system/journald-nnp.conf" /etc/systemd/journald.conf.d/nnp.conf
systemctl restart systemd-journald
apt-config dump | grep -e '^Unattended-Upgrade::Automatic-Reboot' -e '^APT::Periodic::Unattended-Upgrade' \
    || echo "WARN: apt-config shows no unattended-upgrade settings"
systemd-analyze cat-config systemd/journald.conf | grep -e '^SystemMaxUse' -e '^MaxRetentionSec' \
    || echo "WARN: journald shows no nnp limits"
see "no apt errors, a Caddy version line (v2.x), Automatic-Reboot \"true\" / Automatic-Reboot-Time \"05:30\" / Unattended-Upgrade \"1\", and SystemMaxUse=200M / MaxRetentionSec=1month"

step "3/9 user and directories"
id nnp >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/nnp --shell /usr/sbin/nologin nnp
for p in /var/lib/nnp/photos /var/lib/nnp/export /var/lib/nnp/export/current; do no_symlink "$p"; done
install -d -o nnp -g nnp -m 750 /var/lib/nnp /var/lib/nnp/photos /var/lib/nnp/export /var/lib/nnp/export/current
install -d -m 700 /etc/nnp /var/lib/nnp-backup /var/lib/nnp-backup/staging
# A running Caddy only picks up a new group on restart, so step 6 always
# restarts it (/etc/group cannot tell whether the live process has nnp yet).
id -nG caddy | grep -qw nnp || usermod -aG nnp caddy
id nnp
id -nG caddy
see "uid/gid lines for nnp, and 'nnp' among caddy's groups"

step "4/9 python venv"
[ -x "$REPO/.venv/bin/python" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q -e "$REPO[deploy]" -c "$REPO/deploy/constraints.txt"
# /opt/nnp is read-only to nnp (ProtectSystem=strict), so byte-compile as root.
"$REPO/.venv/bin/python" -m compileall -q "$REPO/src"
"$REPO/.venv/bin/gunicorn" --version
see "no pip errors and 'gunicorn (version 22.x or 23.x)'"

step "5/9 app environment ($ENV_FILE)"
if [ ! -e "$ENV_FILE" ]; then
    (
        umask 077
        cp "$REPO/deploy/nnp.env.example" "$ENV_FILE"
    )
    sk=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    at=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    sed -i "s/^NNP_SECRET_KEY=\$/NNP_SECRET_KEY=$sk/; s/^NNP_ADMIN_TOKEN=\$/NNP_ADMIN_TOKEN=$at/" "$ENV_FILE"
    FRESH=1
    echo "created $ENV_FILE with a new secret key and admin token"
    # Print now, not only at the end: a later step can abort this run, and
    # every rerun keeps the existing file (FRESH=0), so it would never show.
    # Steps 7/9 print it again on this run as a reminder.
    print_admin_url_once
else
    echo "kept existing $ENV_FILE (this script never overwrites it)"
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
ls -l "$ENV_FILE"
see "on a first install the SAVE THIS ... admin URL line (save it now), then -rw------- 1 root root ... $ENV_FILE"

step "6/9 systemd units and Caddy"
cp "$REPO/deploy/systemd/nnp.service" /etc/systemd/system/
cp "$REPO/deploy/backup/nnp-backup.service" "$REPO/deploy/backup/nnp-backup.timer" /etc/systemd/system/
for f in "$REPO"/deploy/mirror/*.service "$REPO"/deploy/mirror/*.timer \
         "$REPO"/deploy/monitor/*.service "$REPO"/deploy/monitor/*.timer; do
    if [ -e "$f" ]; then cp "$f" /etc/systemd/system/; fi
done
# As nnp, so a link planted under export/ only reaches what nnp can write.
[ -e /var/lib/nnp/export/current/down.html ] \
    || runuser -u nnp -- install -m 640 "$REPO/deploy/caddy/down.html" /var/lib/nnp/export/current/down.html
if [ ! -e /etc/caddy/Caddyfile.dist ] && [ -e /etc/caddy/Caddyfile ]; then
    cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.dist
fi
admin_email=$(envget "$ENV_FILE" NNP_ADMIN_EMAIL)
if [ -n "$admin_email" ]; then
    sed "s|YOUR_EMAIL|$admin_email|" "$REPO/deploy/caddy/Caddyfile" > /etc/caddy/Caddyfile.new
else
    # Let's Encrypt rejects a placeholder contact, so drop the global
    # options block (the only block opening with a bare "{" line) instead.
    sed '/^{$/,/^}$/d' "$REPO/deploy/caddy/Caddyfile" > /etc/caddy/Caddyfile.new
    echo "WARN: NNP_ADMIN_EMAIL not set in $ENV_FILE; Caddy registers with Let's Encrypt without a contact email (no expiry warnings). Set it and rerun to add one."
fi
# Validate before replacing, so a bad file never becomes the live config.
caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new
mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now caddy
# Restart, not reload: a reload keeps the old process, which may predate
# step 3's usermod (e.g. an earlier run died between step 3 and here) and
# then cannot read /var/lib/nnp. Costs a moment of downtime per run.
systemctl restart caddy
systemctl is-active caddy
see "'Valid configuration' from caddy validate, then 'active'"

step "7/9 database"
if [ ! -e "$DB" ]; then
    echo "DB missing. Seed on the laptop (RUNBOOK 1.5) and upload to $DB, then rerun this script."
    print_admin_url_once
    see "this message; nnp is NOT started until the database is in place"
    exit 0
fi
own_db_files
rc=0
SNAPSHOT=0
"$REPO/deploy/nnp-cli" migrate --db "$DB" --check || rc=$?
case "$rc" in
    0) ;;
    1) premigration_backup ;;
    *) die "nnp-cli migrate --check failed (rc=$rc)" ;;
esac
"$REPO/deploy/nnp-cli" migrate --db "$DB"
see "'up to date' (or the names of the migrations just applied)"

step "8/9 start the app"
systemctl enable nnp
systemctl restart nnp
ok=0
for _ in $(seq 20); do
    if curl -fsS -m 2 -o /dev/null http://127.0.0.1:8000/healthz 2>/dev/null; then ok=1; break; fi
    sleep 1
done
if [ "$ok" != 1 ]; then
    journalctl -u nnp -n 40 --no-pager
    echo "ERROR: nnp did not answer /healthz within 20s (log above)." >&2
    if [ -s "$LAST_GOOD" ]; then
        echo "Roll back to the last commit that passed this check (this script reruns" >&2
        echo "pip, the units, migrate and this health check on it):" >&2
        echo "  sudo git -C $REPO checkout $(cat "$LAST_GOOD") && sudo $REPO/deploy/install.sh" >&2
        echo "That leaves $REPO on a detached HEAD. Once the fix is pushed, update with" >&2
        echo "  sudo git -C $REPO checkout main && sudo git -C $REPO pull --ff-only && sudo $REPO/deploy/install.sh" >&2
    else
        echo "No earlier good deploy is recorded in $LAST_GOOD; fix forward." >&2
    fi
    if [ "$rc" = 1 ] && [ "$SNAPSHOT" = 1 ]; then
        echo "This run applied a migration, so older code may not match the new schema;" >&2
        echo "restore the pre-migration snapshot instead (restic snapshots --tag pre-migration;" >&2
        echo "deploy/RUNBOOK.md, restore)." >&2
    elif [ "$rc" = 1 ]; then
        echo "This run applied a migration with NO pre-migration snapshot (backups not" >&2
        echo "enabled), so older code may not match the new schema and there is nothing to restore." >&2
    fi
    exit 1
fi
git -C "$REPO" rev-parse HEAD > "$LAST_GOOD"
echo "local healthz: ok"
if curl -fsS -m 10 -o /dev/null "$SITE/healthz"; then
    echo "public https: ok"
else
    echo "WARN: public https not up yet (DNS or certificate pending; rerun 'systemctl reload caddy' once dig resolves)"
fi
see "'local healthz: ok' and 'public https: ok'"

step "9/9 done"
print_admin_url_once
see "the admin URL above if this was the first install (save it), else nothing more"
