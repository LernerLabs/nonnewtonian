"""Static checks on deploy/system/* (apt and journald drop-ins). The files
only take effect on the server; these tests guard the promises they keep."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEM = ROOT / "deploy/system"


def test_journald_caps_size_and_age_to_match_privacy_copy():
    lines = (SYSTEM / "journald-nnp.conf").read_text(encoding="utf-8").splitlines()
    assert "[Journal]" in lines
    assert "SystemMaxUse=200M" in lines
    # privacy.html: the error log "is kept for a few weeks".
    assert "MaxRetentionSec=1month" in lines
    assert "few weeks" in (ROOT / "src/nonnewtonian/web/templates/privacy.html").read_text(
        encoding="utf-8")


def test_unattended_upgrades_reboot_and_caddy_origin():
    conf = (SYSTEM / "52nnp-unattended-upgrades").read_text(encoding="utf-8")
    assert 'Unattended-Upgrade::Automatic-Reboot "true";' in conf
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "05:30";' in conf
    assert 'APT::Periodic::Unattended-Upgrade "1";' in conf
    assert "dl.cloudsmith.io" in conf
