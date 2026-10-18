# SPDX-License-Identifier: MPL-2.0
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, path):
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 15}
        )

        @event.listens_for(self.engine, "connect")
        def configure(conn, _):
            path.chmod(0o600)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")

    @contextmanager
    def transaction(self, write=True):
        with Session(self.engine, expire_on_commit=False) as db:
            try:
                if write:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
