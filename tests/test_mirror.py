"""Behavior of deploy/mirror/nnp-mirror.sh against a LOCAL bare git repo
standing in for the GitHub mirror (NNP_MIRROR_REMOTE). Nothing touches the
network; ssh, the deploy key and GitHub Pages are only exercised on the
server (drill D4)."""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIRROR_SH = ROOT / "deploy/mirror/nnp-mirror.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")


def _git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def mirror(tmp_path):
    """Returns (export_dir, remote, run). run(*extra_args, **env) -> CompletedProcess."""
    export = tmp_path / "export"
    export.mkdir()
    remote = tmp_path / "remote.git"
    # Isolated git config: no user/system config (signing, hooks) leaks in.
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "NNP_MIRROR_REMOTE": str(remote),
        "NNP_MIRROR_GITDIR": str(tmp_path / "local.git"),
    }
    _git("init", "-q", "--bare", str(remote), env=base_env)

    def run(*extra, **env):
        full = dict(base_env, **env)
        return subprocess.run(["bash", str(MIRROR_SH), str(export), *extra],
                              env=full, capture_output=True, text=True)

    run.env = base_env
    return export, remote, run


def _write_site(export, pages):
    for rel, text in pages.items():
        p = export / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _remote_head(remote, env):
    return _git("--git-dir", str(remote), "rev-parse", "refs/heads/gh-pages", env=env)


def _reachable_objects(remote, env):
    out = _git("--git-dir", str(remote), "rev-list", "--all", "--objects", env=env)
    return {line.split()[0] for line in out.splitlines()}


def _blob_id(text, env):
    return subprocess.run(["git", "hash-object", "--stdin"], input=text, env=env,
                          capture_output=True, text=True, check=True).stdout.strip()


SITE = {"index.html": "home", ".nojekyll": "", "scientists/a/index.html": "A",
        "scientists/b/index.html": "B", "static/style.css": "body{}"}


def test_first_push_makes_one_parentless_commit(mirror):
    export, remote, run = mirror
    _write_site(export, SITE)
    result = run()
    assert result.returncode == 0, result.stderr
    assert "pushed" in result.stdout
    env = run.env
    assert _git("--git-dir", str(remote), "rev-list", "--count", "gh-pages", env=env) == "1"
    files = _git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "gh-pages", env=env)
    assert sorted(files.splitlines()) == sorted(SITE)


def test_unchanged_rerun_does_not_push(mirror):
    export, remote, run = mirror
    _write_site(export, SITE)
    assert run().returncode == 0
    before = _remote_head(remote, run.env)
    result = run()
    assert result.returncode == 0, result.stderr
    assert "mirror unchanged" in result.stdout
    assert _remote_head(remote, run.env) == before


def test_changed_content_replaces_history_with_one_orphan_commit(mirror):
    export, remote, run = mirror
    _write_site(export, SITE)
    assert run().returncode == 0
    env = run.env
    old_commit = _remote_head(remote, env)
    old_blob = _blob_id("B", env)
    assert old_blob in _reachable_objects(remote, env)
    # Take down scientist b; change a.
    shutil.rmtree(export / "scientists/b")
    (export / "scientists/a/index.html").write_text("A v2")
    result = run()
    assert result.returncode == 0, result.stderr
    new_commit = _remote_head(remote, env)
    assert new_commit != old_commit
    assert _git("--git-dir", str(remote), "rev-list", "--count", "--all", env=env) == "1"
    parents = _git("--git-dir", str(remote), "log", "-1", "--format=%P", "gh-pages", env=env)
    assert parents == ""
    reachable = _reachable_objects(remote, env)
    assert old_commit not in reachable
    assert old_blob not in reachable


def test_local_git_dir_keeps_no_objects_after_push(mirror):
    export, remote, run = mirror
    _write_site(export, SITE)
    assert run().returncode == 0
    (export / "index.html").write_text("home v2")
    assert run().returncode == 0
    gitdir = run.env["NNP_MIRROR_GITDIR"]
    counts = _git("--git-dir", gitdir, "count-objects", "-v", env=run.env)
    stats = dict(line.split(": ") for line in counts.splitlines())
    assert stats["count"] == "0" and stats["in-pack"] == "0", stats


def test_shrink_is_refused_unless_allowed(mirror):
    export, remote, run = mirror
    _write_site(export, {f"scientists/s{i}/index.html": str(i) for i in range(10)})
    assert run().returncode == 0
    before = _remote_head(remote, run.env)
    shutil.rmtree(export / "scientists")
    (export / "index.html").write_text("almost empty")
    result = run()
    assert result.returncode != 0
    assert "refusing: export shrank from 10 to 1 files" in result.stderr
    assert _remote_head(remote, run.env) == before
    result = run("--allow-shrink")
    assert result.returncode == 0, result.stderr
    assert _remote_head(remote, run.env) != before


def test_unreachable_remote_fails_without_pushing(mirror, tmp_path):
    export, remote, run = mirror
    _write_site(export, SITE)
    missing = tmp_path / "no-such-remote.git"
    result = run(NNP_MIRROR_REMOTE=str(missing))
    assert result.returncode != 0
    assert "ls-remote failed" in result.stderr
    assert not missing.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the chmod that blocks the remote")
def test_unlistable_existing_remote_is_not_treated_as_first_push(mirror):
    export, remote, run = mirror
    _write_site(export, SITE)
    assert run().returncode == 0
    before = _remote_head(remote, run.env)
    (export / "index.html").write_text("home v2")
    remote.chmod(0)
    try:
        result = run()
    finally:
        remote.chmod(0o755)
    assert result.returncode != 0
    assert "ls-remote failed" in result.stderr
    assert _remote_head(remote, run.env) == before


def test_symlink_in_export_is_refused(mirror, tmp_path):
    export, remote, run = mirror
    _write_site(export, SITE)
    key = tmp_path / "mirror_ed25519"
    key.write_text("PRIVATE KEY")
    (export / "key.txt").symlink_to(key)
    result = run()
    assert result.returncode != 0
    assert "non-regular file" in result.stderr
    assert subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q",
                           "refs/heads/gh-pages"], env=run.env,
                          capture_output=True).returncode != 0


def test_ping_reports_fail_on_refusal(mirror, tmp_path):
    export, remote, run = mirror
    _write_site(export, SITE)
    (export / "link").symlink_to(export / "index.html")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "curl.log"
    (stubs / "curl").write_text('#!/bin/sh\necho "$@" >> "$STUB_LOG"\n')
    (stubs / "curl").chmod(0o755)
    result = run(PATH=f"{stubs}:{os.environ['PATH']}", STUB_LOG=str(log),
                 NNP_MIRROR_PING_URL="https://hc.example/u")
    assert result.returncode != 0
    pings = [line.split()[-1] for line in log.read_text().splitlines()]
    assert pings == ["https://hc.example/u/start", "https://hc.example/u/fail"]
