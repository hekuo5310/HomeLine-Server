# SPDX-License-Identifier: MPL-2.0
"""Isolated bootstrap orchestration tests; not Pi/systemd installation acceptance."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/bootstrap.sh"


def run_shell(body, env=None):
    return subprocess.run(
        ["bash", "-c", 'source "$BOOTSTRAP"\n' + body],
        env={**os.environ, "BOOTSTRAP": str(SCRIPT), **(env or {})},
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.fixture
def tls(tmp_path):
    # Real PEM, CA verification, hostname and key checks use system OpenSSL.
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=phone.test",
            "-addext",
            "subjectAltName=DNS:phone.test",
            "-keyout",
            str(tmp_path / "key.pem"),
            "-out",
            str(tmp_path / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )
    return {
        "CERT_PATH": str(tmp_path / "cert.pem"),
        "KEY_PATH": str(tmp_path / "key.pem"),
        "SSL_CERT_FILE": str(tmp_path / "cert.pem"),
        "EVENTS": str(tmp_path / "events"),
    }


def test_bootstrap_help_and_unknown_option_are_non_mutating():
    help_result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0 and "--non-interactive" in help_result.stdout
    bad = run_shell("check_platform() { exit 99; }; main --invalid")
    assert bad.returncode == 1 and "Unknown option" in bad.stderr


@pytest.mark.parametrize("failure", ["hostname", "untrusted", "key", "ipv4", "ref"])
def test_bootstrap_rejects_bad_inputs_before_source_or_installer(tls, failure):
    env = dict(tls)
    adjustments = {
        "hostname": "HOST=other.test",
        "untrusted": "export SSL_CERT_FILE=/dev/null SSL_CERT_DIR=/nonexistent",
        "key": 'KEY="$CERT_PATH"',
        "ipv4": "GATEWAY=192.168.1.999",
        "ref": "",
    }
    if failure == "ref":
        body = 'check_platform() { exit 99; }; main --ref "$(echo injected)"'
    else:
        body = (
            """HOST=phone.test CERT="$CERT_PATH" KEY="$KEY_PATH" GATEWAY='' MEDIA_ADDRESS=''
"""
            + adjustments[failure]
            + '\nvalidate_values\nvalidate_certificate\ntouch "$EVENTS"'
        )
    result = run_shell(body, env)
    assert result.returncode != 0
    assert not Path(env["EVENTS"]).exists()


def test_bootstrap_success_preserves_arguments_and_cleans_checkout(tls):
    body = """
check_platform() { :; }
acquire_install_lock() { :; }
existing_installation() { return 1; }
ensure_tools() { :; }
fetch_source() {
    mkdir -p "$1/deploy"
    printf '%s\\n' 'printf "%s\\n" "$@" > "$EVENTS"' > "$1/deploy/install.sh"
    printf '%s' "$1" > "$EVENTS.checkout"
}
main --non-interactive --host phone.test --cert "$CERT_PATH" --key "$KEY_PATH" --gateway 192.168.1.20 --media-address 192.168.1.21
"""
    result = run_shell(body, tls)
    assert result.returncode == 0, result.stderr
    assert Path(tls["EVENTS"]).read_text().splitlines() == [
        "--host",
        "phone.test",
        "--cert",
        tls["CERT_PATH"],
        "--key",
        tls["KEY_PATH"],
        "--gateway",
        "192.168.1.20",
        "--media-address",
        "192.168.1.21",
    ]
    checkout = Path(Path(tls["EVENTS"] + ".checkout").read_text())
    assert not checkout.parent.exists()


def test_bootstrap_repeat_checks_existing_without_download_or_reinstall(tls):
    result = run_shell(
        """
check_platform() { :; }
acquire_install_lock() { :; }
existing_installation() { return 0; }
check_existing() { printf checked > "$EVENTS"; }
ensure_tools() { exit 91; }
fetch_source() { exit 92; }
main --non-interactive
""",
        tls,
    )
    assert result.returncode == 0
    assert Path(tls["EVENTS"]).read_text() == "checked"


def test_bootstrap_failure_stops_before_success_and_cleans_checkout(tls):
    result = run_shell(
        """
check_platform() { :; }
acquire_install_lock() { :; }
existing_installation() { return 1; }
ensure_tools() { :; }
fetch_source() { mkdir -p "$1"; printf '%s' "$1" > "$EVENTS.checkout"; }
run_installer() { return 37; }
main --non-interactive --host phone.test --cert "$CERT_PATH" --key "$KEY_PATH"
""",
        tls,
    )
    assert result.returncode == 37
    assert "Installation and readiness checks completed" not in result.stdout
    checkout = Path(Path(tls["EVENTS"] + ".checkout").read_text())
    assert not checkout.parent.exists()


def test_bootstrap_missing_unattended_inputs_cannot_start_install(tls):
    result = run_shell(
        """
check_platform() { :; }
acquire_install_lock() { :; }
existing_installation() { return 1; }
ensure_tools() { touch "$EVENTS"; exit 99; }
main --non-interactive
""",
        tls,
    )
    assert result.returncode == 1
    assert "Missing HOST" in result.stderr
    assert not Path(tls["EVENTS"]).exists()


def test_bootstrap_prepares_http_client_when_neither_curl_nor_wget_exists(tmp_path):
    result = run_shell(
        """
PATH="$TOOLS_DIR"
! command -v curl
! command -v wget
apt-get() {
    for package in "$@"; do
        if [[ $package == curl ]]; then
            printf '#!/bin/sh\\nexit 0\\n' > "$TOOLS_DIR/curl"
            /bin/chmod +x "$TOOLS_DIR/curl"
        fi
    done
}
ensure_tools
command -v curl >/dev/null
""",
        {"TOOLS_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
