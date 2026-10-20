"""Static checks on the M7 deploy files and the copy that depends on them.

Nothing here touches a server: these tests read files in the repo. `caddy
validate` and `systemd-analyze verify` cannot run in this environment and
are RUNBOOK steps for Michael on the server."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/nonnewtonian/web/templates"


def test_privacy_page_states_backup_retention():
    # The privacy promise must match restic's retention (KEEP_MONTHLY=6).
    assert "six months" in (TEMPLATES / "privacy.html").read_text(encoding="utf-8")
