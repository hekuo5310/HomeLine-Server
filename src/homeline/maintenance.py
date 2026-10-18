# SPDX-License-Identifier: MPL-2.0
"""Encrypted backups include the database and the key required to decrypt SIP secrets."""

import base64
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .provisioning import atomic_write

MAGIC = b"HLSBACKUP1\0"
MAXIMUM = 256 * 1024 * 1024


def cipher(password, salt):
    if len(password) < 12:
        raise ValueError("Backup passphrase must have at least 12 characters")
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def backup(settings, config_file, destination, password):
    if destination.exists():
        raise FileExistsError("Refusing to overwrite existing backup")
    with tempfile.TemporaryDirectory() as work:
        target = Path(work) / "state.sqlite3"
        with (
            sqlite3.connect(f"file:{settings.database}?mode=ro", uri=True) as source,
            sqlite3.connect(target) as dest,
        ):
            source.backup(dest)
        if target.stat().st_size > MAXIMUM:
            raise ValueError("Backup exceeds supported 256 MiB size")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(target, "state.sqlite3")
            archive.writestr("master.key", settings.key_file.read_bytes())
            archive.writestr("server.env", config_file.read_bytes())
            for path in sorted((config_file.parent / "asterisk").rglob("*")):
                if not path.is_file():
                    continue
                if path.is_symlink():
                    raise ValueError("Symlinked PBX configuration cannot be backed up")
                archive.write(
                    path, "asterisk/" + str(path.relative_to(config_file.parent / "asterisk"))
                )
            if settings.firebase_credentials:
                archive.writestr("firebase.json", settings.firebase_credentials.read_bytes())
            archive.writestr(
                "manifest.json", json.dumps({"format": 1, "database": "state.sqlite3"})
            )
        salt = os.urandom(16)
        encoded = MAGIC + salt + cipher(password, salt).encrypt(buffer.getvalue())
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())


def restore(settings, config_file, source, password):
    if source.stat().st_size > MAXIMUM * 2:
        raise ValueError("Archive too large")
    data = source.read_bytes()
    if not data.startswith(MAGIC):
        raise ValueError("Unknown backup format")
    salt = data[len(MAGIC) : len(MAGIC) + 16]
    plain = cipher(password, salt).decrypt(data[len(MAGIC) + 16 :])
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        names = set(archive.namelist())
        fixed = {"state.sqlite3", "master.key", "server.env", "manifest.json", "firebase.json"}
        pbx_names = {
            name
            for name in names
            if re.fullmatch(
                r"asterisk/(?:(?:generated/)?[A-Za-z0-9_-]+\.conf|tls/server\.(?:crt|key))", name
            )
        }
        if (
            not {"state.sqlite3", "master.key", "server.env", "manifest.json"} <= names
            or names - fixed - pbx_names
        ):
            raise ValueError("Unexpected backup members")
        if "firebase.json" in names and not settings.firebase_credentials:
            raise ValueError("Configure Firebase credentials destination before restore")
        if (
            len(archive.infolist()) != len(names)
            or sum(x.file_size for x in archive.infolist()) > MAXIMUM + 1024 * 1024
        ):
            raise ValueError("Invalid backup sizes")
        if json.loads(archive.read("manifest.json"))["format"] != 1:
            raise ValueError("Unknown backup version")
        # Read only fixed member names; never extract archive-controlled paths.
        db_data, key, config = (
            archive.read("state.sqlite3"),
            archive.read("master.key"),
            archive.read("server.env"),
        )
        verifier = Fernet(key.strip())
        with tempfile.TemporaryDirectory() as work:
            candidate = Path(work) / "candidate.sqlite3"
            candidate.write_bytes(db_data)
            with sqlite3.connect(candidate) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Corrupt database")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Broken foreign keys")
                if db.execute("SELECT version_num FROM alembic_version").fetchone()[0] != "0001":
                    raise ValueError("Unsupported schema")
                for (secret,) in db.execute("SELECT sip_secret FROM devices"):
                    verifier.decrypt(secret.encode())
        # Caller must stop the service and acquire its singleton lock before restore.
        stamp = ".before-restore-" + str(__import__("time").time_ns())
        for path in (settings.database, settings.key_file, config_file):
            if path.exists():
                atomic_write(Path(str(path) + stamp), path.read_bytes(), 0o600)
        for suffix in ("-wal", "-shm"):
            path = Path(str(settings.database) + suffix)
            if path.exists():
                os.replace(path, Path(str(path) + stamp))
        for name in pbx_names:
            target = config_file.parent / name
            if target.exists():
                atomic_write(Path(str(target) + stamp), target.read_bytes(), 0o600)
            atomic_write(target, archive.read(name), 0o640)
        atomic_write(settings.key_file, key, 0o600)
        atomic_write(config_file, config, 0o600)
        if "firebase.json" in names:
            atomic_write(settings.firebase_credentials, archive.read("firebase.json"), 0o600)
        atomic_write(settings.database, db_data, 0o600)
