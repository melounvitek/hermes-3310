"""install-asterisk.sh renders the SIP account from Hermes's .env without running it as a script."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install-asterisk.sh"


def render(tmp_path, env_text: str, conf: str = "pjsip.conf") -> str:
    (tmp_path / ".env").write_text("SIP_SERVER=sip.example.com\nSIP_USER=alice\n" + env_text)
    subprocess.run([str(SCRIPT), "--render-only"], check=True, capture_output=True,
                   env={**os.environ, "HERMES_HOME": str(tmp_path)})
    return (tmp_path / "phone" / "asterisk" / conf).read_text()


@pytest.mark.parametrize("line, password", [
    ("SIP_PASSWORD=plain", "plain"),
    ("SIP_PASSWORD=plain  # the account password", "plain"),
    ('SIP_PASSWORD="abc#def" # account', "abc#def"),
    ("SIP_PASSWORD='it is $HOME'", "it is $HOME"),
    ('export SIP_PASSWORD="say \\"hi\\""', 'say "hi"'),
    ("SIP_PASSWORD=$(touch pwned)", "$(touch pwned)"),
])
def test_sip_password_is_read_like_hermes_reads_env(tmp_path, line, password):
    assert f"password={password}\n" in render(tmp_path, line + "\n")
    assert not (tmp_path / "pwned").exists() and not Path("pwned").exists()


def test_rendered_files_are_private(tmp_path):
    render(tmp_path, "SIP_PASSWORD=secret\n")
    conf = tmp_path / "phone" / "asterisk"

    assert oct(conf.stat().st_mode & 0o777) == "0o700"
    assert oct((conf / "pjsip.conf").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "phone" / "outgoing").stat().st_mode & 0o777) == "0o700"  # call files: whoever writes, dials


def test_calls_from_hermes_show_the_caller_id_the_plugin_sets_per_call(tmp_path):
    dialplan = render(tmp_path, "SIP_PASSWORD=secret\n", "extensions.conf")

    assert "Set(CALLERID(num)=${CALLER_ID})" in dialplan  # from the call file: no re-run after a change


def test_asterisk_takes_calls_only_from_the_provider_address(tmp_path):
    pjsip = render(tmp_path, "SIP_PASSWORD=secret\n")

    # Only by the provider's IP: a username or the registration's line parameter could come from anyone.
    # (Checked against Asterisk 23.5: an INVITE from another address gets 401 and never reaches the dialplan.)
    assert "endpoint_identifier_order=ip\n" in pjsip and "identify_by=ip\n" in pjsip
    assert "line=" not in pjsip
    registration = pjsip.split("[sip-reg]")[1].split("\n[")[0]
    assert "endpoint=" not in registration  # Asterisk refuses a registration endpoint without line support
