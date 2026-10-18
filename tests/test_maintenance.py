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
