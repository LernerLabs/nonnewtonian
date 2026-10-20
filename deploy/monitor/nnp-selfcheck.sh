#!/usr/bin/env bash
# Daily self-check (dead-man switch) for NonNewtonian Physicists. Runs as root
# from nnp-selfcheck.timer; config from /etc/nnp/monitor.env.
#
# Every check runs; failures are collected into one message. All OK -> ping
# NNP_SELFCHECK_PING_URL; any failure -> POST the message to .../fail. If the
# box is down nothing pings at all, and healthchecks.io alerts on the silence.
#
# Tested only with stub systemctl/curl/openssl/df/swapon/find
# (tests/test_selfcheck.py); the real run on the server is the check.
set -euo pipefail

: "${NNP_SELFCHECK_PING_URL:?set NNP_SELFCHECK_PING_URL (healthchecks.io) in /etc/nnp/monitor.env}"
HOST=nonnewtonian.org
DATA=/var/lib/nnp

msg=""
bad() { msg="${msg}$*"$'\n'; }

units="nnp caddy nnp-backup.timer"
if systemctl is-enabled --quiet nnp-mirror.timer 2>/dev/null; then units="$units nnp-mirror.timer"; fi
for u in $units; do
    systemctl is-active --quiet "$u" || bad "$u is not active"
done

curl -fsS -m 10 -o /dev/null --resolve "$HOST:443:127.0.0.1" "https://$HOST/healthz" 2>/dev/null \
    || bad "https://$HOST/healthz via 127.0.0.1 failed"

# Certificate valid for 14+ more days. Caddy renews at ~30 days left, so this
# fires only after ~2 weeks of failed renewals.
if ! echo | openssl s_client -connect 127.0.0.1:443 -servername "$HOST" 2>/dev/null \
        | openssl x509 -noout -checkend 1209600 >/dev/null 2>&1; then
    bad "TLS certificate for $HOST expires within 14 days (or could not be read)"
fi

pcent=$(df --output=pcent "$DATA" | tail -n 1 | tr -dc 0-9) || pcent=""
if [ -z "$pcent" ] || [ "$pcent" -ge 90 ]; then bad "disk for $DATA is ${pcent:-?}% full"; fi

[ -n "$(swapon --noheadings)" ] || bad "no swap active"

# Caddy serves photos/ and export/ (both writable by nnp), and its file_server
# follows symlinks, so an app compromise could plant a link to Caddy's TLS key
# and fetch it over HTTPS. The app never writes symlinks there. KNOWN
# LIMITATION: this is daily DETECTION, not prevention -- a planted link can be
# served for up to a day before this alerts.
links=$(find "$DATA/photos" "$DATA/export" -type l 2>&1) || links="(find failed) $links"
if [ -n "$links" ]; then
    bad "symlinks under $DATA/photos or $DATA/export (first 20):"$'\n'"$(printf '%s\n' "$links" | head -n 20)"
fi

failed=$(systemctl --failed --no-legend --plain) || failed="(systemctl --failed did not run)"
[ -z "$failed" ] || bad "failed units: $failed"

# The ping URL goes to curl as a config on stdin (-K -), never on argv: argv is
# world-readable in /proc, and anyone holding the URL can fake success pings.
# (Config quoting: the URL must not contain a double quote or backslash;
# healthchecks.io URLs never do.)
# The /fail body is not secret and stays on argv.
if [ -z "$msg" ]; then
    printf 'url = "%s"\n' "$NNP_SELFCHECK_PING_URL" | curl -fsS -m 10 --retry 3 -o /dev/null -K - || true
    echo "selfcheck ok"
else
    printf 'url = "%s/fail"\n' "$NNP_SELFCHECK_PING_URL" \
        | curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "$msg" -K - || true
    printf 'selfcheck FAILED:\n%s' "$msg" >&2
    exit 1
fi
