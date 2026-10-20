"""Behavior of deploy/monitor/nnp-selfcheck.sh, run against stub systemctl,
curl, openssl, df, swapon and find executables. Nothing real is checked or
pinged; the real tools only run on the server."""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SELFCHECK_SH = ROOT / "deploy/monitor/nnp-selfcheck.sh"
PING = "https://hc-ping.example/uuid"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUBS = {
    # INACTIVE: space-separated units that are not active; MIRROR_ENABLED=1
    # makes nnp-mirror.timer enabled; FAILED_UNITS: `systemctl --failed` output.
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
case "$1" in
  is-active) for u in $INACTIVE; do [ "$u" = "$3" ] && exit 3; done; exit 0 ;;
  is-enabled) [ "${MIRROR_ENABLED:-}" = 1 ] && exit 0; exit 1 ;;
  --failed) [ -n "${FAILED_UNITS:-}" ] && echo "$FAILED_UNITS"; exit 0 ;;
esac
exit 0
""",
    # Records every call (a --data-raw body may span lines; the tests split the
    # log on "curl "). Only the healthz probe can fail (HEALTHZ_RC).
    # Logs "curl ARGV -- URL", URL read from a `-K -` config on stdin.
    "curl": """#!/bin/sh
url=""
for a in "$@"; do [ "$a" = "-K" ] && url=$(sed -n 's/^url = "\\(.*\\)"$/\\1/p'); done
echo "curl $* -- $url" >> "$STUB_LOG"
for a in "$@"; do case "$a" in *healthz) exit "${HEALTHZ_RC:-0}" ;; esac; done
exit 0
""",
    "openssl": """#!/bin/sh
cat >/dev/null
[ "$1" = x509 ] && exit "${CERT_RC:-0}"
exit 0
""",
    "df": """#!/bin/sh
echo "Use%"
echo " ${DISK_PCENT:-42}%"
""",
    # FIND_OUT: what `find ... -type l` prints; FIND_RC: its exit status.
    "find": """#!/bin/sh
echo "find $*" >> "$STUB_LOG"
[ -n "${FIND_OUT:-}" ] && printf '%s\\n' "$FIND_OUT"
exit "${FIND_RC:-0}"
""",
    "swapon": """#!/bin/sh
[ "${NO_SWAP:-}" = 1 ] || echo "/swapfile file 1024M 0B -2"
""",
}


def _run(tmp_path, **env_extra):
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        p = stubs / name
        p.write_text(body)
        p.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(log),
           "NNP_SELFCHECK_PING_URL": PING}
    env.update(env_extra)
    env = {k: v for k, v in env.items() if v is not None}
    result = subprocess.run(["bash", str(SELFCHECK_SH)], env=env, capture_output=True, text=True)
    return result, log.read_text()


def _ping_calls(log):
    return [c for c in log.split("curl ")[1:] if "--resolve" not in c]


def test_all_ok_pings_success_only(tmp_path):
    result, log = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    pings = _ping_calls(log)
    assert len(pings) == 1
    assert pings[0].rstrip().endswith(PING) and "--data-raw" not in pings[0]
    for unit in ("nnp", "caddy", "nnp-backup.timer"):
        assert f"systemctl is-active --quiet {unit}\n" in log
    assert "nnp-mirror.timer" not in log.replace("is-enabled --quiet nnp-mirror.timer", "")


def test_healthz_goes_through_local_caddy_with_sni(tmp_path):
    _, log = _run(tmp_path)
    healthz = [c for c in log.split("curl ")[1:] if "healthz" in c]
    assert healthz and "--resolve nonnewtonian.org:443:127.0.0.1" in healthz[0]
    assert "https://nonnewtonian.org/healthz" in healthz[0]


@pytest.mark.parametrize("env,needle", [
    ({"INACTIVE": "nnp-backup.timer"}, "nnp-backup.timer is not active"),
    ({"INACTIVE": "caddy"}, "caddy is not active"),
    ({"HEALTHZ_RC": "22"}, "healthz via 127.0.0.1 failed"),
    ({"CERT_RC": "1"}, "TLS certificate"),
    ({"DISK_PCENT": "93"}, "93% full"),
    ({"NO_SWAP": "1"}, "no swap active"),
    ({"FAILED_UNITS": "nnp-backup.service loaded failed failed backup"},
     "failed units: nnp-backup.service"),
    ({"MIRROR_ENABLED": "1", "INACTIVE": "nnp-mirror.timer"}, "nnp-mirror.timer is not active"),
    ({"FIND_OUT": "/var/lib/nnp/photos/aa/" + "b" * 64 + ".jpg"},
     "/var/lib/nnp/photos/aa/" + "b" * 64 + ".jpg"),
    ({"FIND_RC": "1"}, "(find failed)"),
])
def test_each_failure_posts_to_fail_with_the_reason(tmp_path, env, needle):
    result, log = _run(tmp_path, **env)
    assert result.returncode != 0
    pings = _ping_calls(log)
    assert len(pings) == 1
    assert "--data-raw" in pings[0] and needle in pings[0]
    assert pings[0].rstrip().endswith(PING + "/fail")
    assert needle in result.stderr


def test_several_failures_go_in_one_message(tmp_path):
    _, log = _run(tmp_path, INACTIVE="nnp caddy", DISK_PCENT="95")
    pings = _ping_calls(log)
    assert len(pings) == 1
    for needle in ("nnp is not active", "caddy is not active", "95% full"):
        assert needle in pings[0]


def test_missing_ping_url_refuses(tmp_path):
    result, log = _run(tmp_path, NNP_SELFCHECK_PING_URL=None)
    assert result.returncode != 0
    assert "NNP_SELFCHECK_PING_URL" in result.stderr
    assert log == ""


@pytest.mark.parametrize("env", [{}, {"NO_SWAP": "1"}])
def test_ping_url_never_on_curl_argv(tmp_path, env):
    # argv is world-readable in /proc; the URL is a secret (review #27).
    _, log = _run(tmp_path, **env)
    pings = _ping_calls(log)
    assert len(pings) == 1
    argv, _, url = pings[0].rstrip().rpartition(" -- ")
    assert "-K -" in argv and "hc-ping" not in argv
    assert url.startswith(PING)


def test_symlink_scan_covers_photos_and_export(tmp_path):
    # Caddy follows symlinks in both trees (review #22).
    _, log = _run(tmp_path)
    assert "find /var/lib/nnp/photos /var/lib/nnp/export -type l\n" in log
