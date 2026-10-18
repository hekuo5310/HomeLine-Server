# SPDX-License-Identifier: MPL-2.0
import time
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def ident():
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    extension: Mapped[str] = mapped_column(String(8), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    status: Mapped[str] = mapped_column(String(16), default="active")
    device_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (
        CheckConstraint("device_limit IS NULL OR device_limit>=0"),
        CheckConstraint("role IN ('user','admin')"),
        CheckConstraint("status IN ('active','disabled')"),
    )


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    platform: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_seen_at: Mapped[float] = mapped_column(Float, default=time.time)
    sip_secret: Mapped[str] = mapped_column(Text)
    sip_expires_at: Mapped[float] = mapped_column(Float, default=0)
    push_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    push_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    __table_args__ = (CheckConstraint("status IN ('active','revoked')"),)


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    origin_device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    winner_device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    remote: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(16), default="waiting", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    deadline: Mapped[float] = mapped_column(Float)
    claimed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_channel: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    bridge_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Leg(Base):
    __tablename__ = "call_legs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    channel_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    sip_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="callee")
    state: Mapped[str] = mapped_column(String(16), default="waiting")
    __table_args__ = (
        UniqueConstraint("call_id", "device_id"),
        UniqueConstraint("device_id", "sip_call_id"),
    )


class OutboundTask(Base):
    __tablename__ = "outbound_tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    destination: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), default="waiting", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), nullable=True, unique=True)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    __table_args__ = (Index("queue_order", "state", "priority", "created_at", "id"),)


class LineLease(Base):
    __tablename__ = "line_leases"
    line_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), nullable=True, unique=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    channel_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    state: Mapped[str] = mapped_column(String(16), default="unknown")
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Idempotency(Base):
    __tablename__ = "idempotency"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    operation: Mapped[str] = mapped_column(String(160))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (UniqueConstraint("device_id", "operation", "key"),)


class DeviceEvent(Base):
    __tablename__ = "device_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    stage: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (UniqueConstraint("call_id", "device_id", "stage"),)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    kind: Mapped[str] = mapped_column(String(32))
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), nullable=True, index=True)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_error: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ident)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    start: Mapped[float] = mapped_column(Float)
    count: Mapped[int] = mapped_column(Integer)
