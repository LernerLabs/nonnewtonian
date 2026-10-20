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
