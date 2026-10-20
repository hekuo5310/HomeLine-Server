# SPDX-License-Identifier: MPL-2.0
import importlib.util
import json
import stat
from pathlib import Path

import pytest

from homeline.networking import load_turn_config, rtp_configuration

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def turn(tmp_path):
    path = tmp_path / "turn.json"
    data = json.loads((ROOT / "deploy/turn.example.json").read_text())
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path, data


def test_turn_configuration_reaches_pbx_and_client(tmp_path, turn):
    path, data = turn
    config = module("configure_turn", "deploy/configure.py")
    cert, key = tmp_path / "cert", tmp_path / "key"
    cert.write_text("fixture-only-cert")
    key.write_text("fixture-only-key")
    root = tmp_path / "target"
    config.configure(root, "phone.example", str(cert), str(key), turn_config=path)
    rtp = (root / "etc/homeline/asterisk/rtp.conf").read_text()
    assert "turnaddr=turn.example.com:3478" in rtp
    assert "turnpassword=" + data["password"] in rtp
    assert stat.S_IMODE((root / "etc/homeline/asterisk/rtp.conf").stat().st_mode) == 0o640
    from homeline.config import Settings

    settings = Settings(_env_file=root / "etc/homeline/server.env")
    assert settings.ice_servers == data["ice_servers"]
    before = (root / "etc/homeline/server.env").read_bytes()
    config.configure(root, "phone.example", str(cert), str(key), turn_config=path)
    assert before == (root / "etc/homeline/server.env").read_bytes()


@pytest.mark.parametrize(
    "invalid", ["public", "injection", "bad_url", "missing", "tls_server", "secure_udp"]
)
def test_turn_rejects_unsafe_or_incompatible_input(turn, invalid):
    path, data = turn
    if invalid == "public":
        path.chmod(0o644)
    else:
        if invalid == "injection":
            data["password"] = "secret\n[section]"
        if invalid == "bad_url":
            data["ice_servers"][0]["urls"] = "https://turn.example"
        if invalid == "secure_udp":
            data["ice_servers"][0]["urls"] = "turns:turn.example:5349?transport=udp"
        if invalid == "missing":
            del data["ice_servers"]
        if invalid == "tls_server":
            data["server"] = "turns:turn.example:5349"
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError) as rejected:
        load_turn_config(path)
    assert "secret" not in str(rejected.value)


def test_default_rtp_settings_keep_lan_compatibility():
    assert "turnpassword" not in rtp_configuration()
    assert "icesupport=yes" in rtp_configuration()


@pytest.mark.parametrize("invalid", ["public", "reserved", "invalid_json"])
def test_dns_secrets_file_is_data_only_and_private(tmp_path, invalid):
    acme = module("acme_credentials", "deploy/acme-setup.py")
    path = tmp_path / "dns.json"
    path.write_text('{"CF_Token":"private-value"}')
    path.chmod(0o600)
    if invalid == "public":
        path.chmod(0o644)
    if invalid == "reserved":
        path.write_text('{"BASH_ENV":"private-value"}')
    if invalid == "invalid_json":
        path.write_text("not-json-private-value")
    with pytest.raises(ValueError) as error:
        acme.credentials(path)
    assert "private-value" not in str(error.value)


def test_acme_orchestration_pins_source_and_keeps_tokens_out_of_arguments(tmp_path, monkeypatch):
    acme = module("acme_setup", "deploy/acme-setup.py")
    for name in ("HOME", "CONFIG", "CERTS", "HELPERS", "SYSTEMD"):
        target = tmp_path / name
        target.mkdir()
        monkeypatch.setattr(acme, name, target)
    monkeypatch.setattr(acme, "SERVER_ENV", tmp_path / "no-server.env")
    monkeypatch.setattr(acme, "NGINX", tmp_path / "no-nginx")
    monkeypatch.setattr(acme, "require_root", lambda: None)
    path = tmp_path / "dns.json"
    path.write_text('{"CF_Token":"private-token"}')
    path.chmod(0o600)
    calls = []

    def run(*args, env=None, cwd=None):
        values = [str(a) for a in args]
        calls.append((values, env, cwd))
        if values[:1] == ["git"] and "checkout" in values:
            (Path(values[2]) / "LICENSE.md").parent.mkdir(parents=True)
            (Path(values[2]) / "LICENSE.md").write_text("upstream fixture")
        if "--install" in values:
            assert cwd is not None
            (acme.HOME / "acme.sh").write_text("fixture")
            (acme.HOME / "dnsapi").mkdir()
            (acme.HOME / "dnsapi/dns_cf.sh").write_text("fixture")
        if "--install-cert" in values:
            (acme.CERTS / "key.pem").write_text("test-key")
            (acme.CERTS / "fullchain.pem").write_text("test-cert")

    monkeypatch.setattr(acme, "run", run)
    monkeypatch.setattr(
        acme.subprocess,
        "check_output",
        lambda args, **kw: acme.ACME_COMMIT + "\n" if "rev-parse" in args else "v3.1.6\n",
    )
    acme.setup("phone.example", "user@example.com", "dns_cf", path)
    assert any(acme.ACME_COMMIT in values for values, _, _ in calls)
    issue = next(c for c in calls if "--issue" in c[0])
    assert issue[1]["CF_Token"] == "private-token"
    assert all("private-token" not in " ".join(values) for values, _, _ in calls)
    assert any("homeline-acme.timer" in values for values, _, _ in calls)
    assert stat.S_IMODE((acme.CERTS / "key.pem").stat().st_mode) == 0o600
    assert (acme.HOME / "UPSTREAM-LICENSE.md").exists()


@pytest.mark.parametrize("mode", ["busy", "idle", "readiness_failed"])
def test_certificate_activation_defers_busy_calls_and_preserves_pending_on_failure(tmp_path, mode):
    import os
    import subprocess

    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
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
            str(tls_dir / "key"),
            "-out",
            str(tls_dir / "cert"),
        ],
        check=True,
        capture_output=True,
    )
    env = dict(
        os.environ,
        CERT_PATH=str(tls_dir / "cert"),
        KEY_PATH=str(tls_dir / "key"),
        SSL_CERT_FILE=str(tls_dir / "cert"),
        EVENTS=str(tmp_path / "events"),
        MODE=mode,
    )
    script = """source "$1"
require_root() { :; }
pending_installation() { return 0; }
acquire_lock() { :; }
certificate_paths() { CERT=$CERT_PATH; KEY=$KEY_PATH; HOST=phone.test; }
idle() { [[ $MODE != busy ]]; }
systemctl() { printf 'systemctl %s\\n' "$*" >> "$EVENTS"; }
install() { printf 'install\\n' >> "$EVENTS"; }
nginx() { :; }
curl() { [[ $MODE != readiness_failed ]]; }
sleep() { :; }
rm() { printf 'clear-pending\\n' >> "$EVENTS"; }
activate
"""
    result = subprocess.run(
        ["bash", "-c", script, "test", str(ROOT / "deploy/activate-cert.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    events = Path(env["EVENTS"]).read_text() if Path(env["EVENTS"]).exists() else ""
    if mode == "busy":
        assert result.returncode == 0 and not events
    elif mode == "idle":
        assert result.returncode == 0
        assert events.index("systemctl stop homeline") < events.index("install")
        assert "clear-pending" in events and "restart homeline-asterisk" in events
    else:
        assert result.returncode != 0
        assert "clear-pending" not in events
        assert events.endswith("systemctl start homeline\n")


@pytest.mark.parametrize(
    "url",
    [
        "turn:turn.example:3478",
        "turn:turn.example:3478?transport=udp",
        "turn:turn.example:3478?transport=tcp",
        "turns:turn.example:5349",
        "turns:turn.example:5349?transport=tcp",
    ],
)
def test_supported_client_turn_transports_remain_accepted(turn, url):
    path, data = turn
    data["ice_servers"][0]["urls"] = url
    path.write_text(json.dumps(data))
    assert load_turn_config(path)["ice_servers"][0]["urls"] == url


def test_private_turn_file_owned_by_another_user_is_rejected(turn, monkeypatch):
    from homeline import networking

    path, _ = turn
    owner = path.stat().st_uid
    monkeypatch.setattr(networking.os, "geteuid", lambda: owner + 1)
    with pytest.raises(ValueError, match="owned by the installer"):
        load_turn_config(path)


def test_turn_config_must_be_regular_and_not_a_symbolic_link(turn, tmp_path):
    import os

    path, _ = turn
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_turn_config(link)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="private"):
        load_turn_config(fifo)


def test_turn_ownership_check_and_read_use_same_open_file(turn, tmp_path, monkeypatch):
    from homeline import networking

    path, original = turn
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"untrusted":"replacement"}')
    replacement.chmod(0o600)
    real_fstat = networking.os.fstat

    def replace_path_after_check(descriptor):
        info = real_fstat(descriptor)
        replacement.replace(path)
        return info

    monkeypatch.setattr(networking.os, "fstat", replace_path_after_check)
    assert load_turn_config(path) == original
