"""Static checks on the M7 deploy files and the copy that depends on them.

Nothing here touches a server: these tests read files in the repo. `caddy
validate` and `systemd-analyze verify` cannot run in this environment and
are RUNBOOK steps for Michael on the server."""
import io
import pathlib
import re
import runpy
import shutil
import subprocess

import pytest
from PIL import Image

from nonnewtonian.photos import store_bytes

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
TEMPLATES = ROOT / "src/nonnewtonian/web/templates"
CADDYFILE = (DEPLOY / "caddy/Caddyfile").read_text(encoding="utf-8")


def test_privacy_page_states_backup_retention():
    # The privacy promise must match restic's retention (KEEP_MONTHLY=6).
    assert "six months" in (TEMPLATES / "privacy.html").read_text(encoding="utf-8")


SHELL_SCRIPTS = sorted(DEPLOY.rglob("*.sh")) + [DEPLOY / "nnp-cli"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(DEPLOY)))
def test_shell_scripts_parse(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_gunicorn_conf_is_one_loopback_process_without_access_log():
    conf = runpy.run_path(str(DEPLOY / "gunicorn.conf.py"))
    assert conf["workers"] == 1
    assert conf["worker_class"] == "gthread"
    assert conf["bind"].startswith("127.0.0.1")
    assert conf["accesslog"] is None
    assert "max_requests" not in conf


def test_systemd_unit_restarts_forever_with_memory_cap():
    unit = (DEPLOY / "systemd/nnp.service").read_text(encoding="utf-8")
    for needle in ("Restart=always", "StartLimitIntervalSec=0", "MemoryMax=",
                   "nonnewtonian.web.wsgi:app", "EnvironmentFile=/etc/nnp/nnp.env"):
        assert needle in unit


def test_caddyfile_has_no_access_log_and_keeps_error_status():
    # Tokens live in URL paths: no `log` directive anywhere.
    directives = [line.split()[0] for line in CADDYFILE.splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    assert "log" not in directives
    assert "status {err.status_code}" in CADDYFILE


def _caddy_photo_regex() -> re.Pattern:
    m = re.search(r"@photo path_regexp (\S+)", CADDYFILE)
    assert m, "no @photo matcher in the Caddyfile"
    return re.compile(m.group(1))


def test_caddy_photo_regex_matches_what_store_bytes_writes(tmp_path):
    rx = _caddy_photo_regex()
    for mode, fmt in (("RGB", "JPEG"), ("RGBA", "PNG")):
        buf = io.BytesIO()
        Image.new(mode, (8, 8)).save(buf, format=fmt)
        stored = store_bytes(buf.getvalue(), tmp_path)
        url_path = "/photos/" + stored.path.relative_to(tmp_path).as_posix()
        assert rx.fullmatch(url_path), url_path
    for bad in ("/photos/../app.db", "/photos/ab/app.db",
                "/photos/ab/" + "a" * 64 + ".svg", "/photos/ab/" + "A" * 64 + ".jpg"):
        assert not rx.fullmatch(bad), bad


def test_env_example_leaves_secrets_for_install_sh_to_fill():
    env = (DEPLOY / "nnp.env.example").read_text(encoding="utf-8")
    # install.sh's sed only fills these exact empty lines.
    assert re.search(r"^NNP_SECRET_KEY=$", env, re.M)
    assert re.search(r"^NNP_ADMIN_TOKEN=$", env, re.M)


def test_constraints_do_not_pin_gunicorn():
    # gunicorn was never installed where the pins were generated; the
    # pyproject `deploy` extra bounds it instead.
    pins = [line for line in (DEPLOY / "constraints.txt").read_text().splitlines()
            if line and not line.startswith("#")]
    assert pins and not any(p.lower().startswith("gunicorn") for p in pins)


def test_deploy_text_files_are_ascii():
    offenders = []
    for f in sorted(DEPLOY.rglob("*")):
        if f.is_file():
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if any(ord(ch) > 127 for ch in line):
                    offenders.append(f"{f.relative_to(ROOT)}:{i}")
    assert not offenders, offenders


INSTALL_SH = (DEPLOY / "install.sh").read_text(encoding="utf-8")


def _install_step(n: int) -> str:
    """The text of install.sh's `step "n/9 ..."` section."""
    m = re.search(rf'^step "{n}/9 .*?(?=^step "|\Z)', INSTALL_SH, re.M | re.S)
    assert m, f"no step {n}/9 in install.sh"
    return m.group(0)


def _code_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_install_always_restarts_caddy_so_it_picks_up_group_nnp():
    # M7 review #4: whether the live Caddy has group nnp cannot be read from
    # /etc/group, and a reload keeps the old process.
    step6 = _code_lines(_install_step(6))
    assert "systemctl restart caddy" in step6
    assert not [line for line in step6 if line.startswith("systemctl reload caddy")]
    assert "CADDY_RESTART" not in INSTALL_SH


def _install_function(name: str) -> str:
    """The source of one shell function from install.sh."""
    m = re.search(rf"^{name}\(\) \{{(?: [^\n]*\}}$|\n.*?^\}}$)", INSTALL_SH, re.M | re.S)
    assert m, f"no function {name} in install.sh"
    return m.group(0)


# Stubs record their argv; runuser then runs the command after `--` with
# RUNUSER=1 set, so a test can tell which commands ran "as nnp".
INSTALL_STUBS = {
    "runuser": 'echo "runuser $*" >> "$STUB_LOG"\nshift 3\nRUNUSER=1 exec "$@"\n',
    "chown": 'echo "chown $*" >> "$STUB_LOG"\n',
    "chmod": 'echo "chmod RUNUSER=${RUNUSER:-0} $*" >> "$STUB_LOG"\n',
}


def _run_install_functions(tmp_path, names, body, stubs=INSTALL_STUBS, env_extra=None):
    """Run `body` in bash after defining install.sh's functions `names`,
    with stub executables first on PATH. Returns (result, stub log lines)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, text in stubs.items():
        p = bindir / name
        p.write_text("#!/bin/sh\n" + text)
        p.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    script = "set -euo pipefail\n" + "\n".join(_install_function(n) for n in names) + "\n" + body
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "STUB_LOG": str(log)}
    env.update(env_extra or {})
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    return result, log.read_text().splitlines()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_own_db_files_chowns_without_following_links_and_chmods_as_nnp(tmp_path):
    # M7 review #5: root used to run `chown nnp:nnp app.db*; chmod 600 app.db`.
    db = tmp_path / "app.db"
    db.touch()
    (tmp_path / "app.db-wal").touch()
    result, calls = _run_install_functions(
        tmp_path, ["die", "no_symlink", "own_db_files"], f"DB={db}\nown_db_files\n")
    assert result.returncode == 0, result.stderr
    assert f"chown -h nnp:nnp {db}" in calls
    assert f"chown -h nnp:nnp {db}-wal" in calls
    assert not any("-shm" in c for c in calls)  # absent files are skipped
    assert f"chmod RUNUSER=1 600 {db}" in calls


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("planted", ["app.db", "app.db-wal", "app.db-shm"])
def test_own_db_files_refuses_a_planted_symlink(tmp_path, planted):
    target = tmp_path / "shadow"
    target.touch()
    db = tmp_path / "app.db"
    if planted != "app.db":
        db.touch()
    (tmp_path / planted).symlink_to(target)
    result, calls = _run_install_functions(
        tmp_path, ["die", "no_symlink", "own_db_files"], f"DB={db}\nown_db_files\n")
    assert result.returncode != 0
    assert "is a symlink" in result.stderr
    assert not any(c.startswith(("chmod", "runuser")) for c in calls)
    assert not any(c.endswith(str(tmp_path / planted)) for c in calls)


def test_install_refuses_symlinked_nnp_dirs_and_writes_down_html_as_nnp():
    # M7 review #21: root installed down.html through export/current, which
    # nnp can swap for a link; step 3's `install -d` runs through the same paths.
    step3 = _install_step(3)
    guard = step3.index("no_symlink")
    for p in ("/var/lib/nnp/photos", "/var/lib/nnp/export", "/var/lib/nnp/export/current"):
        assert p in step3[:step3.index("\n", guard)], p
    assert guard < step3.index("install -d -o nnp")
    step6 = _install_step(6)
    assert 'runuser -u nnp -- install -m 640 "$REPO/deploy/caddy/down.html"' in step6
    assert "install -o nnp" not in step6


MIRROR_KEY_STUBS = dict(
    INSTALL_STUBS,
    id="exit 0\n",
    install='echo "install $*" >> "$STUB_LOG"\nfor last; do :; done\nmkdir -p "$last"\n',
    **{
        "ssh-keygen": 'echo "ssh-keygen RUNUSER=${RUNUSER:-0} $*" >> "$STUB_LOG"\n'
                      'if [ "$1" = -q ]; then for last; do :; done; : > "$last"; '
                      'echo "ssh-ed25519 AAAA nnp" > "$last.pub"; fi\n',
        "ssh-keyscan": 'echo "ssh-keyscan RUNUSER=${RUNUSER:-0} $*" >> "$STUB_LOG"\n'
                       'echo "github.com ssh-ed25519 AAAAgh"\n',
    },
)


def _run_mirror_key(tmp_path):
    # Point the function's hard-coded /var/lib/nnp at a scratch dir.
    fn = _install_function("mirror_key").replace("/var/lib/nnp", str(tmp_path / "nnp"))
    names = ["die", "no_symlink", "step", "see"]
    body = fn + "\nmirror_key\n"
    return _run_install_functions(tmp_path, names, body, stubs=MIRROR_KEY_STUBS,
                                  env_extra={"REPO": "/opt/nnp"})


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_mirror_key_touches_nnps_ssh_dir_only_as_nnp(tmp_path):
    # M7 review #6: root used to write, chown and chmod known_hosts there.
    (tmp_path / "nnp").mkdir()
    result, calls = _run_mirror_key(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "nnp/.ssh/known_hosts").read_text() == "github.com ssh-ed25519 AAAAgh\n"
    assert any(c.startswith("ssh-keyscan RUNUSER=1") for c in calls)
    assert all("RUNUSER=1" in c for c in calls if c.startswith(("ssh-keyscan", "ssh-keygen")))
    assert not any(c.startswith(("chown", "chmod")) for c in calls)
    assert "ssh-ed25519 AAAA nnp" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_mirror_key_refuses_a_symlinked_ssh_dir(tmp_path):
    (tmp_path / "nnp").mkdir()
    (tmp_path / "root").mkdir()
    (tmp_path / "nnp/.ssh").symlink_to(tmp_path / "root")
    result, calls = _run_mirror_key(tmp_path)
    assert result.returncode != 0
    assert "is a symlink" in result.stderr
    assert not any(c.startswith(("install", "runuser")) for c in calls)


def test_caddy_fallback_redirects_directories_to_a_trailing_slash():
    # M7 review #11: the export's relative links assume a base URL ending in
    # "/", but try_files served /scientists/index.html at /scientists. Only a
    # text check; `caddy validate` on the server is the real one.
    errors = CADDYFILE[CADDYFILE.index("handle_errors"):]
    matcher = re.search(r"@dir_no_slash \{\n(.*?)\n\t*\}\n", errors, re.S)
    assert matcher, "no @dir_no_slash matcher in handle_errors"
    assert "not path */" in matcher.group(1)
    assert "file {path}/index.html" in matcher.group(1)
    assert errors.index("redir @dir_no_slash {path}/") < errors.index("try_files")


def test_install_rollback_hint_reruns_install_and_returns_to_main():
    # M7 review #15: the old hint left /opt/nnp detached and skipped pip,
    # the units and the health check.
    step8 = _install_step(8)
    assert 'checkout $(cat "$LAST_GOOD") && sudo $REPO/deploy/install.sh' in step8
    assert "checkout main && sudo git -C $REPO pull --ff-only" in step8
    assert "systemctl restart nnp\" >&2" not in step8


def _run_premigration_backup(tmp_path, *, timer_enabled=True, backup_rc=0):
    benv = tmp_path / "nnp-backup.env"
    benv.write_text("RESTIC_REPOSITORY=b2:test:nnp\n")
    backup = tmp_path / "nnp-backup.sh"
    backup.write_text('#!/bin/sh\necho "backup tag=${NNP_BACKUP_TAG:-} repo=$RESTIC_REPOSITORY"'
                      f' >> "$STUB_LOG"\nexit {backup_rc}\n')
    backup.chmod(0o755)
    fn = (_install_function("premigration_backup")
          .replace("/etc/nnp-backup.env", str(benv))
          .replace("/opt/nnp/deploy/backup/nnp-backup.sh", str(backup)))
    stubs = {"systemctl": f'echo "systemctl $*" >> "$STUB_LOG"\nexit {0 if timer_enabled else 1}\n'}
    body = fn + '\nSNAPSHOT=0\npremigration_backup\necho "SNAPSHOT=$SNAPSHOT"\n'
    return _run_install_functions(tmp_path, ["die"], body, stubs=stubs)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_premigration_backup_passes_the_tag_and_records_success(tmp_path):
    # M7 review #24: the snapshot used the nightly tag and could be pruned.
    result, calls = _run_premigration_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "backup tag=pre-migration repo=b2:test:nnp" in calls
    assert result.stdout.strip().endswith("SNAPSHOT=1")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_premigration_backup_failure_stops_before_migrating(tmp_path):
    result, _ = _run_premigration_backup(tmp_path, backup_rc=1)
    assert result.returncode != 0
    assert "pre-migration backup failed" in result.stderr
    assert "SNAPSHOT=" not in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_premigration_backup_skipped_leaves_no_snapshot_claim(tmp_path):
    result, calls = _run_premigration_backup(tmp_path, timer_enabled=False)
    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("backup") for c in calls)
    assert result.stdout.strip().endswith("SNAPSHOT=0")


def test_install_restore_hint_only_when_a_snapshot_was_taken():
    step8 = _install_step(8)
    hint = step8.index("restore the pre-migration snapshot")
    guard = step8.rindex("if ", 0, hint)
    assert '[ "$SNAPSHOT" = 1 ]' in step8[guard:hint]


# M7 review #19: nnp.env is read by systemd (literal values) and was also
# sourced as shell, where `KEY=abcd efgh` runs `efgh` and `$x` expands.
SYSTEMD_STYLE_ENV = """# comment
NNP_SMTP_PASSWORD=abcd efgh $x $$y
#NNP_ADMIN_EMAIL=commented@example.org
NNP_ADMIN_EMAIL='me@example.org'
NNP_MAIL_FROM="Physics <p@example.org>"
NNP_ADMIN_EMAIL=last@example.org
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("key, want", [
    ("NNP_SMTP_PASSWORD", "abcd efgh $x $$y"),
    ("NNP_ADMIN_EMAIL", "last@example.org"),
    ("NNP_MAIL_FROM", "Physics <p@example.org>"),
    ("NNP_SMTP_HOST", ""),
])
def test_envget_reads_values_literally_like_systemd(tmp_path, key, want):
    env = tmp_path / "nnp.env"
    env.write_text(SYSTEMD_STYLE_ENV)
    result, _ = _run_install_functions(tmp_path, ["envget"], f'envget "{env}" {key}\n', stubs={})
    assert result.returncode == 0, result.stderr
    assert result.stdout == want
    assert not result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_nnp_cli_loads_env_values_literally_like_systemd(tmp_path):
    env = tmp_path / "nnp.env"
    env.write_text(SYSTEMD_STYLE_ENV.replace("NNP_ADMIN_EMAIL=last@example.org\n", ""))
    cli = tmp_path / "nnp-cli"
    cli.write_text((DEPLOY / "nnp-cli").read_text()
                   .replace("/etc/nnp/nnp.env", str(env))
                   .replace("cd /var/lib/nnp", f"cd {tmp_path}"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in {
        "id": "echo 0\n",
        "runuser": "shift 3\nprintf '%s|' \"$*\" \"$NNP_SMTP_PASSWORD\" \"$NNP_ADMIN_EMAIL\" \"$NNP_MAIL_FROM\"\n",
    }.items():
        (bindir / name).write_text("#!/bin/sh\n" + text)
        (bindir / name).chmod(0o755)
    result = subprocess.run(["sh", str(cli), "migrate", "--check"], capture_output=True, text=True,
                            env={"PATH": f"{bindir}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("/opt/nnp/.venv/bin/nonnewtonian migrate --check|abcd efgh $x $$y|"
                             "me@example.org|Physics <p@example.org>|")


def test_install_prints_the_admin_url_as_soon_as_it_creates_the_env_file():
    # M7 review #20: a first run that died after step 5 never showed it, and
    # every rerun keeps the file, so FRESH stays 0.
    step5 = _install_step(5)
    created = step5.index("FRESH=1")
    assert "print_admin_url_once" in step5[created:step5.index("else", created)]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_print_admin_url_once_only_on_the_creating_run(tmp_path):
    env = tmp_path / "nnp.env"
    env.write_text("NNP_ADMIN_TOKEN=tok123\n")
    names = ["envget", "print_admin_url_once"]
    head = f"ENV_FILE={env}\nSITE=https://nonnewtonian.org\n"
    fresh, _ = _run_install_functions(tmp_path, names, head + "FRESH=1\nprint_admin_url_once\n", stubs={})
    assert fresh.returncode == 0, fresh.stderr
    assert "SAVE THIS IN YOUR PASSWORD MANAGER NOW: https://nonnewtonian.org/admin/tok123" in fresh.stdout
    rerun, _ = _run_install_functions(tmp_path, names, head + "FRESH=0\nprint_admin_url_once\n", stubs={})
    assert rerun.returncode == 0 and rerun.stdout == ""
