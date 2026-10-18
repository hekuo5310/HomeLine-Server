# SPDX-License-Identifier: MPL-2.0
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from homeline import models as m
from homeline.config import Settings
from homeline.db import Database
from homeline.security import Vault, hasher, verify_password


def migrated(path):
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    return cfg


def test_migration_roundtrip_and_constraints(tmp_path):
    path = tmp_path / "state.sqlite3"
    cfg = migrated(path)
    migrated(path)
    database = Database(path)
    assert "line_leases" in inspect(database.engine).get_table_names()
    with pytest.raises(IntegrityError), database.transaction() as db:
        db.add(m.Device(user_id="missing", name="bad", platform="android", sip_secret="encrypted"))
    with database.transaction() as db:
        db.add(m.User(username="u", extension="1001", password_hash="hash"))
    with pytest.raises(IntegrityError), database.transaction() as db:
        db.add(m.User(username="other", extension="1001", password_hash="hash"))
    database.engine.dispose()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    with database.transaction(write=False) as db:
        assert list(db.scalars(select(m.User))) == []


def test_encrypted_secret_and_key_permissions(tmp_path):
    path = tmp_path / "key"
    path.write_bytes(Fernet.generate_key())
    path.chmod(0o600)
    vault = Vault(path)
    encoded = vault.encrypt("private")
    assert "private" not in encoded and vault.decrypt(encoded) == "private"
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        Vault(path)


def test_password_hash_and_invalid_credentials():
    encoded = hasher.hash("test-only-long-password")
    assert verify_password(encoded, "test-only-long-password")
    assert not verify_password(encoded, "wrong")
    assert not verify_password(None, "wrong")


def test_management_and_sip_urls_fail_closed(tmp_path):
    base = {
        "ari_password": "test-only",
        "sip_wss_url": "wss://phone.example/ws",
        "sip_domain": "phone.example",
    }
    assert Settings(**base).ari_url.startswith("http://127.0.0.1")
    with pytest.raises(ValueError):
        Settings(**{**base, "ari_url": "http://public.example/ari"})
    with pytest.raises(ValueError):
        Settings(**{**base, "sip_wss_url": "ws://phone.example/ws"})


def test_validation_messages_do_not_echo_cli_or_configuration_secrets():
    from pydantic import ValidationError

    from homeline.schemas import CreateUser

    with pytest.raises(ValidationError) as settings_error:
        Settings(
            _env_file=None, ari_password="private-ari-do-not-echo", sip_wss_url="wss://localhost/ws"
        )
    assert "private-ari-do-not-echo" not in str(settings_error.value)
    with pytest.raises(ValidationError) as user_error:
        CreateUser(username="admin", extension="1001", password="secret")
    assert "input_value" not in str(user_error.value)
    assert "'secret'" not in str(user_error.value)
