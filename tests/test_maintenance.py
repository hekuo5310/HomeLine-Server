# SPDX-License-Identifier: MPL-2.0
import importlib.util
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from test_api import login

from homeline.maintenance import backup, restore
from homeline.models import User


def test_encrypted_backup_restore_and_wrong_password(app, tmp_path):
    a, c = app
    service = a.state.service
    settings = service.settings
    login(c)
    config = tmp_path / "server.env"
    config.write_text("test config, secret not exposed")
    archive = tmp_path / "backup.hls"
    backup(settings, config, archive, "test-backup-passphrase")
    assert archive.stat().st_mode & 0o077 == 0
    assert b"test config" not in archive.read_bytes()
    with pytest.raises(FileExistsError):
        backup(settings, config, archive, "test-backup-passphrase")
    with service.database.transaction() as db:
        db.scalar(select(User).where(User.username == "bob")).status = "disabled"
    service.database.engine.dispose()
    before = settings.database.read_bytes()
    with pytest.raises(InvalidToken):
        restore(settings, config, archive, "incorrect-passphrase")
    assert settings.database.read_bytes() == before
    restore(settings, config, archive, "test-backup-passphrase")
    with sqlite3.connect(settings.database) as db:
        assert db.execute("SELECT status FROM users WHERE username='bob'").fetchone()[0] == "active"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list(tmp_path.glob("*.before-restore-*"))


def test_configuration_repeat_preserves_credentials_and_data(tmp_path):
    path = Path(__file__).parents[1] / "deploy/configure.py"
    spec = importlib.util.spec_from_file_location("configure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cert = tmp_path / "cert.pem"
    cert.write_text("test certificate placeholder, not used for TLS")
    key = tmp_path / "private.pem"
    key.write_text("test key placeholder, not used for TLS")
    module.configure(tmp_path, "phone.example", str(cert), str(key), "192.168.1.20")
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    db = tmp_path / "var/lib/homeline/homeline.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"never overwrite existing data")
    module.configure(tmp_path, "phone.example", str(cert), str(key), "192.168.1.20")
    assert all((tmp_path / p).read_bytes() == data for p, data in before.items())
    assert db.read_bytes() == b"never overwrite existing data"
    assert (tmp_path / "etc/homeline/server.env").stat().st_mode & 0o077 == 0
    assert "password=" in (tmp_path / "etc/homeline/asterisk/ari.conf").read_text()
    with pytest.raises(ValueError):
        module.configure(tmp_path, "bad\ninclude=evil", "/cert", "/key")


def test_deployment_preserves_secure_sip_upstream(tmp_path):
    path = Path(__file__).parents[1] / "deploy/configure.py"
    spec = importlib.util.spec_from_file_location("secure_configure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("test")
    key.write_text("test")
    module.configure(tmp_path, "phone.example", str(cert), str(key))
    http = (tmp_path / "etc/homeline/asterisk/http.conf").read_text()
    nginx = (tmp_path / "etc/nginx/sites-available/homeline").read_text()
    assert "tlsenable=yes" in http and "tlsbindaddr=127.0.0.1:8089" in http
    assert "proxy_pass https://127.0.0.1:8089/ws" in nginx and "proxy_ssl_verify on" in nginx
    assert "proxy_ssl_name phone.example" in nginx


def test_existing_database_without_master_key_fails_closed(tmp_path):
    path = Path(__file__).parents[1] / "deploy/configure.py"
    spec = importlib.util.spec_from_file_location("missing_key_configure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = tmp_path / "var/lib/homeline/homeline.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="original master key"):
        module.configure(tmp_path, "phone.example", "/cert", "/key")
    assert db.read_bytes() == b"existing" and not (tmp_path / "etc/homeline/master.key").exists()


def test_repeated_backup_restore_excludes_preserved_pbx_snapshots(app, tmp_path):
    a, c = app
    settings = a.state.service.settings
    config = tmp_path / "server.env"
    config.write_text("original config")
    pbx = tmp_path / "asterisk"
    pbx.mkdir()
    (pbx / "extensions.conf").write_text("original dialplan")
    (pbx / "custom.extra.conf").write_text("custom configuration")
    archive = tmp_path / "first.hls"
    backup(settings, config, archive, "test-backup-passphrase")
    a.state.service.database.engine.dispose()
    (pbx / "extensions.conf").write_text("changed dialplan")
    restore(settings, config, archive, "test-backup-passphrase")
    assert list(pbx.glob("*.before-restore-*"))
    second = tmp_path / "second.hls"
    backup(settings, config, second, "test-backup-passphrase")
    restore(settings, config, second, "test-backup-passphrase")
    assert (pbx / "extensions.conf").read_text() == "original dialplan"
    assert (pbx / "custom.extra.conf").read_text() == "custom configuration"


def test_restore_write_failure_rolls_back_all_files(app, tmp_path, monkeypatch):
    import homeline.maintenance as maintenance

    a, c = app
    settings = a.state.service.settings
    config = tmp_path / "server.env"
    config.write_text("old config")
    archive = tmp_path / "backup.hls"
    backup(settings, config, archive, "test-backup-passphrase")
    config.write_text("current config")
    a.state.service.database.engine.dispose()
    before = {p: p.read_bytes() for p in (settings.database, settings.key_file, config)}
    original = maintenance.atomic_write
    failed = False

    def fail_once(path, data, mode=0o640):
        nonlocal failed
        if path == settings.database and not failed:
            failed = True
            raise OSError("injected write failure")
        return original(path, data, mode)

    monkeypatch.setattr(maintenance, "atomic_write", fail_once)
    with pytest.raises(OSError, match="injected"):
        restore(settings, config, archive, "test-backup-passphrase")
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not maintenance.restore_marker(settings).exists()


def test_interrupted_restore_blocks_startup_and_retry(app, tmp_path, monkeypatch):
    import homeline.maintenance as maintenance
    from homeline.api import create_app

    a, c = app
    settings = a.state.service.settings
    config = tmp_path / "server.env"
    config.write_text("old config")
    archive = tmp_path / "backup.hls"
    backup(settings, config, archive, "test-backup-passphrase")
    a.state.service.database.engine.dispose()
    original = maintenance.atomic_write

    def interrupted(path, data, mode=0o640):
        if path == settings.database:
            raise KeyboardInterrupt("simulated process interruption")
        return original(path, data, mode)

    monkeypatch.setattr(maintenance, "atomic_write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        restore(settings, config, archive, "test-backup-passphrase")
    assert maintenance.restore_marker(settings).exists()
    with pytest.raises(RuntimeError, match="Incomplete restore"):
        create_app(settings, a.state.runtime)
    with pytest.raises(RuntimeError, match="Incomplete previous restore"):
        restore(settings, config, archive, "test-backup-passphrase")


def test_backup_is_published_only_after_complete_write(app, tmp_path, monkeypatch):
    import homeline.maintenance as maintenance

    a, c = app
    config = tmp_path / "server.env"
    config.write_text("test configuration")
    destination = tmp_path / "backup.hls"

    def fail_link(source, target):
        raise OSError("injected publish failure")

    monkeypatch.setattr(maintenance.os, "link", fail_link)
    with pytest.raises(OSError, match="publish failure"):
        backup(a.state.service.settings, config, destination, "test-backup-passphrase")
    assert not destination.exists()
    assert list(tmp_path.glob(".homeline-backup-*")) == []
