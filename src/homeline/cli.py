# SPDX-License-Identifier: MPL-2.0
import argparse
import asyncio
import fcntl
import getpass
import json
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from .config import Settings
from .db import Database
from .maintenance import backup, check_backup, restore
from .models import User
from .schemas import CreateUser
from .security import hasher


def migrate(settings):
    root = Path(__file__).resolve().parents[2]
    if not (root / "migrations").is_dir():
        root = Path(sys.prefix) / "share/homeline"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + str(settings.database))
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        command.upgrade(cfg, "head")
    finally:
        os.umask(old)
    settings.database.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(prog="homeline")
    parser.add_argument("--config", type=Path, default=Path("/etc/homeline/server.env"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("doctor")
    admin = sub.add_parser("init-admin")
    admin.add_argument("--username", default="admin")
    admin.add_argument("--extension", default="1001")
    for name in ("backup", "restore"):
        p = sub.add_parser(name)
        p.add_argument("archive", type=Path)
        p.add_argument(
            "--check",
            action="store_true",
            help="Validate without writing files or requiring service shutdown",
        )
        p.add_argument("--passphrase-file", type=Path)
    args = parser.parse_args()
    settings = Settings(_env_file=args.config)
    if args.command == "doctor":
        from .diagnostics import diagnose

        try:
            print(json.dumps(asyncio.run(diagnose(settings))))
        except Exception as exc:
            raise SystemExit("diagnostic_failed: " + type(exc).__name__) from None
        return
    if args.command == "migrate":
        migrate(settings)
        return
    if args.command == "init-admin":
        password = getpass.getpass("New administrator password (12+ characters): ")
        if password != getpass.getpass("Repeat password: "):
            parser.error("Passwords do not match")
        data = CreateUser(
            username=args.username, password=password, extension=args.extension, role="admin"
        )
        database = Database(settings.database)
        with database.transaction() as db:
            if db.scalar(select(User.id).where(User.role == "admin")):
                parser.error("Administrator already exists; use protected API")
            db.add(
                User(
                    username=data.username,
                    password_hash=hasher.hash(data.password),
                    extension=data.extension,
                    role="admin",
                )
            )
        print("Administrator created; no password written to console or configuration.")
        return
    if args.passphrase_file:
        if args.passphrase_file.stat().st_mode & 0o077:
            parser.error("Passphrase file must be mode 0600")
        password = args.passphrase_file.read_text().rstrip("\n")
    else:
        password = getpass.getpass("Backup passphrase (12+ characters): ")
    if args.check:
        if args.command == "backup":
            check_backup(settings, args.config, args.archive, password)
        else:
            restore(settings, args.config, args.archive, password, check_only=True)
        print(args.command + " preflight passed; no files changed")
        return
    if args.command == "backup":
        backup(settings, args.config, args.archive, password)
    else:
        with settings.lock_file.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("Stop HomeLine before restoring")
            restore(settings, args.config, args.archive, password)
    print(args.command + " complete")


if __name__ == "__main__":
    main()
