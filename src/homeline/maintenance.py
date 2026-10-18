# SPDX-License-Identifier: MPL-2.0
"""Encrypted backups include the database and the key required to decrypt SIP secrets."""

import base64
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .provisioning import atomic_write
from .security import Vault

MAGIC = b"HLSBACKUP1\0"
MAXIMUM = 256 * 1024 * 1024


def pbx_member(name):
    return (
        re.fullmatch(
            r"asterisk/(?:(?:generated/)?[A-Za-z0-9_][A-Za-z0-9_.-]*\.conf|tls/server\.(?:crt|key))",
            name,
        )
        is not None
    )


def restore_marker(settings):
    return settings.database.parent / "restore-in-progress.json"


def replace_restore_files(settings, replacements):
    # Ordinary I/O failures roll back the complete set. A persistent marker blocks
    # startup after an interrupted process; retained snapshots allow offline recovery.
    marker = restore_marker(settings)
    if marker.exists():
        raise RuntimeError(
            "Incomplete previous restore; inspect restore-in-progress.json before retrying"
        )
    stamp = ".before-restore-" + str(__import__("time").time_ns())
    previous = {}
    for path in replacements:
        if path.is_symlink():
            raise ValueError("Restore target must not be a symlink")
        if path.exists():
            info = path.stat()
            snapshot = Path(str(path) + stamp)
            atomic_write(snapshot, path.read_bytes(), 0o600)
            previous[path] = (snapshot, info.st_mode & 0o777, info.st_uid, info.st_gid)
        else:
            previous[path] = None
    atomic_write(
        marker,
        json.dumps(
            {"snapshots": {str(p): str(v[0]) if v else None for p, v in previous.items()}}
        ).encode(),
        0o600,
    )
    try:
        for path, value in replacements.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                data, mode = value
                atomic_write(path, data, mode)
                old = previous[path]
                if old:
                    os.chown(path, old[2], old[3])
    except Exception:
        try:
            for path, old in previous.items():
                if old:
                    atomic_write(path, old[0].read_bytes(), old[1])
                    os.chown(path, old[2], old[3])
                else:
                    path.unlink(missing_ok=True)
        except Exception as exc:
            raise RuntimeError(
                "Restore and rollback failed; retain restore marker and recover saved snapshots offline"
            ) from exc
        marker.unlink()
        raise
    marker.unlink()


def cipher(password, salt):
    if len(password) < 12:
        raise ValueError("Backup passphrase must have at least 12 characters")
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def publish_backup(destination, data):
    fd, temporary = tempfile.mkstemp(prefix=".homeline-backup-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Publish an already-complete inode, exclusively: no overwrite or partial archive.
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_backup(settings, config_file, destination, password):
    if len(password) < 12:
        raise ValueError("Backup passphrase must have at least 12 characters")
    if destination.exists():
        raise FileExistsError("Refusing to overwrite existing backup")
    if not destination.parent.is_dir():
        raise FileNotFoundError("Backup destination directory does not exist")
    if not config_file.is_file() or not settings.database.is_file():
        raise FileNotFoundError("Configuration or database missing")
    if restore_marker(settings).exists():
        raise RuntimeError("Incomplete restore; resolve it before backup")
    vault = Vault(settings.key_file)
    with sqlite3.connect(f"file:{settings.database}?mode=ro", uri=True) as db:
        for secret, token in db.execute("SELECT sip_secret,push_token FROM devices"):
            vault.decrypt(secret)
            if token:
                vault.decrypt(token)


def backup(settings, config_file, destination, password):
    check_backup(settings, config_file, destination, password)
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
                member = "asterisk/" + str(path.relative_to(config_file.parent / "asterisk"))
                if pbx_member(member):
                    archive.write(path, member)
            if settings.firebase_credentials:
                archive.writestr("firebase.json", settings.firebase_credentials.read_bytes())
            archive.writestr(
                "manifest.json", json.dumps({"format": 1, "database": "state.sqlite3"})
            )
            if sum(x.file_size for x in archive.infolist()) > MAXIMUM + 1024 * 1024:
                raise ValueError("Backup entries exceed supported restore size")
        salt = os.urandom(16)
        encoded = MAGIC + salt + cipher(password, salt).encrypt(buffer.getvalue())
        publish_backup(destination, encoded)


def restore(settings, config_file, source, password, *, check_only=False):
    if restore_marker(settings).exists():
        raise RuntimeError(
            "Incomplete previous restore; inspect restore-in-progress.json before retrying"
        )
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
        pbx_names = {name for name in names if pbx_member(name)}
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
            with closing(sqlite3.connect(candidate)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Corrupt database")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Broken foreign keys")
                if db.execute("SELECT version_num FROM alembic_version").fetchone()[0] != "0001":
                    raise ValueError("Unsupported schema")
                for (secret,) in db.execute("SELECT sip_secret FROM devices"):
                    verifier.decrypt(secret.encode())
                if not check_only:
                    with db:
                        # A point-in-time backup cannot know what ran or was revoked
                        # after its capture. Quarantine rather than replay authority.
                        db.execute("UPDATE users SET status='disabled'")
                        db.execute("UPDATE sessions SET revoked=1")
                        db.execute(
                            "UPDATE devices SET status='revoked',sip_expires_at=0,push_token=NULL,push_hash=NULL"
                        )
                        db.execute(
                            "UPDATE calls SET state='failed',failure_reason='backup_restored',revision=revision+1,updated_at=? WHERE state NOT IN ('ended','cancelled','failed','timeout')",
                            (time.time(),),
                        )
                        db.execute(
                            "UPDATE call_legs SET state='ended' WHERE state IN ('waiting','ringing','up')"
                        )
                        db.execute(
                            "UPDATE outbound_tasks SET state='failed',failure_reason='backup_restored',revision=revision+1 WHERE state IN ('waiting','ringing','dialing','connected')"
                        )
                        db.execute(
                            "UPDATE outbox SET status='done' WHERE status IN ('pending','running')"
                        )
                        db.execute(
                            "UPDATE line_leases SET state='unknown',call_id=NULL,channel_id=NULL"
                        )
                        db.execute(
                            "INSERT INTO audit_events(id,actor_id,action,resource_id,created_at) VALUES (?,NULL,'backup_restored',NULL,?)",
                            (uuid.uuid4().hex, time.time()),
                        )
            db_data = candidate.read_bytes()
        if check_only:
            return
        # Caller must stop both services and acquire the singleton lock.
        replacements = {
            config_file.parent / name: (archive.read(name), 0o640) for name in pbx_names
        }
        replacements[settings.pjsip_file] = (
            b"; Disabled after backup restore; recover administrator and sign in again.\n",
            0o640,
        )
        replacements[settings.key_file] = (key, 0o600)
        replacements[config_file] = (config, 0o600)
        if "firebase.json" in names:
            replacements[settings.firebase_credentials] = (archive.read("firebase.json"), 0o600)
        replacements[settings.database] = (db_data, 0o600)
        for suffix in ("-wal", "-shm"):
            replacements[Path(str(settings.database) + suffix)] = None
        replace_restore_files(settings, replacements)
