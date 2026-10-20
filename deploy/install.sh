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
# Read one variable out of a root-only env file without exporting the rest.
envget() { ( set -a; . "$1"; eval "printf '%s' \"\${$2:-}\"" ); }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 $*"
[ "$(readlink -f "$0")" = "$REPO/deploy/install.sh" ] \
    || die "install.sh must run from $REPO/deploy/install.sh (clone the repo to $REPO)"

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
    install -d -o nnp -g nnp -m 700 "$d"
    [ -e "$d/mirror_ed25519" ] \
        || runuser -u nnp -- ssh-keygen -q -t ed25519 -N '' -C nnp-mirror@nonnewtonian.org -f "$d/mirror_ed25519"
    # Recorded once (trust on first use); delete known_hosts to re-record.
    if [ ! -s "$d/known_hosts" ]; then
        ssh-keyscan -t ed25519 github.com > "$d/known_hosts.new" 2>/dev/null
        [ -s "$d/known_hosts.new" ] || die "ssh-keyscan github.com returned nothing (network?)"
        mv "$d/known_hosts.new" "$d/known_hosts"
    fi
    chown nnp:nnp "$d/known_hosts"
    chmod 644 "$d/known_hosts"
    echo "Deploy key (GitHub: mglerner/nonnewtonian-mirror -> Settings -> Deploy keys -> Add, WITH write access):"
    cat "$d/mirror_ed25519.pub"
    echo "github.com host key recorded in $d/known_hosts:"
    ssh-keygen -lf "$d/known_hosts"
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

case "${1:-}" in
    "") ;;
    --enable-backups) enable_backups; exit 0 ;;
    --mirror-key) mirror_key; exit 0 ;;
    --enable-mirror) enable_mirror; exit 0 ;;
    --enable-selfcheck) die "$1 is not implemented yet (a later M7 package adds it)" ;;
    *) die "usage: $0 [--enable-backups | --mirror-key | --enable-mirror]" ;;
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

step "2/9 packages"
apt-get update -q
apt-get install -y -q python3-venv git sqlite3 restic curl
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
see "no apt errors and a Caddy version line (v2.x)"

step "3/9 user and directories"
id nnp >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/nnp --shell /usr/sbin/nologin nnp
install -d -o nnp -g nnp -m 750 /var/lib/nnp /var/lib/nnp/photos /var/lib/nnp/export /var/lib/nnp/export/current
install -d -m 700 /etc/nnp /var/lib/nnp-backup /var/lib/nnp-backup/staging
# A running Caddy only picks up a new group on restart (step 6).
CADDY_RESTART=0
if ! id -nG caddy | grep -qw nnp; then
    usermod -aG nnp caddy
    CADDY_RESTART=1
fi
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
else
    echo "kept existing $ENV_FILE (this script never overwrites it)"
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
ls -l "$ENV_FILE"
see "-rw------- 1 root root ... $ENV_FILE"

step "6/9 systemd units and Caddy"
cp "$REPO/deploy/systemd/nnp.service" /etc/systemd/system/
cp "$REPO/deploy/backup/nnp-backup.service" "$REPO/deploy/backup/nnp-backup.timer" /etc/systemd/system/
for f in "$REPO"/deploy/mirror/*.service "$REPO"/deploy/mirror/*.timer \
         "$REPO"/deploy/monitor/*.service "$REPO"/deploy/monitor/*.timer; do
    if [ -e "$f" ]; then cp "$f" /etc/systemd/system/; fi
done
[ -e /var/lib/nnp/export/current/down.html ] \
    || install -o nnp -g nnp -m 640 "$REPO/deploy/caddy/down.html" /var/lib/nnp/export/current/down.html
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
if [ "$CADDY_RESTART" = 1 ]; then
    systemctl restart caddy
else
    systemctl reload caddy
fi
systemctl is-active caddy
see "'Valid configuration' from caddy validate, then 'active'"

step "7/9 database"
if [ ! -e "$DB" ]; then
    echo "DB missing. Seed on the laptop (RUNBOOK 1.5) and upload to $DB, then rerun this script."
    print_admin_url_once
    see "this message; nnp is NOT started until the database is in place"
    exit 0
fi
chown nnp:nnp "$DB"*
chmod 600 "$DB"
rc=0
"$REPO/deploy/nnp-cli" migrate --db "$DB" --check || rc=$?
case "$rc" in
    0) ;;
    1)
        if systemctl is-enabled --quiet nnp-backup.timer; then
            echo "pending migrations: taking a backup snapshot first"
            systemctl start nnp-backup.service || die "pre-migration backup failed; not migrating (journalctl -u nnp-backup.service)"
        else
            echo "WARN: pending migrations and backups are not enabled yet; no pre-migration snapshot"
        fi
        ;;
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
        echo "Rollback to the last commit that passed this check:" >&2
        echo "  sudo git -C $REPO checkout $(cat "$LAST_GOOD") && sudo systemctl restart nnp" >&2
        echo "If this run applied a migration, the old code may not match the new schema;" >&2
        echo "restore the pre-migration snapshot instead (deploy/RUNBOOK.md, restore)." >&2
    else
        echo "No earlier good deploy is recorded in $LAST_GOOD; fix forward." >&2
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
