# SPDX-License-Identifier: MPL-2.0
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import models as m
from .security import digest, verify_password

TERMINAL = {"ended", "cancelled", "failed", "timeout"}


class Problem(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def audit(db, actor, action, resource=None):
    db.add(m.Audit(actor_id=actor, action=action, resource_id=resource))


def change(call, state):
    if call.state in TERMINAL:
        return
    call.state, call.updated_at = state, time.time()
    call.revision += 1


def command(db, kind, call_id=None, device_id=None, **payload):
    db.add(m.Outbox(kind=kind, call_id=call_id, device_id=device_id, payload=json.dumps(payload)))


@dataclass
class Identity:
    user: m.User
    device: m.Device
    session: m.AuthSession
    token: str


class Service:
    def __init__(self, database, settings, vault):
        self.database, self.settings, self.vault = database, settings, vault

    def authenticate(self, db, token):
        session = db.scalar(select(m.AuthSession).where(m.AuthSession.token_hash == digest(token)))
        if not session or session.revoked or session.expires_at <= time.time():
            raise Problem(401, "session_expired")
        device = db.get(m.Device, session.device_id)
        user = db.get(m.User, device.user_id)
        if device.status != "active" or user.status != "active":
            raise Problem(403, "access_revoked")
        return Identity(user, device, session, token)

    def rate(self, key, maximum, period=60):
        with self.database.transaction() as db:
            key, now = digest(key), time.time()
            bucket = db.get(m.RateBucket, key)
            if not bucket:
                bucket = m.RateBucket(key=key, start=now, count=0)
                db.add(bucket)
            elif now - bucket.start > period:
                bucket.start, bucket.count = now, 0
            bucket.count += 1
            exceeded = bucket.count > maximum
        if exceeded:
            raise Problem(429, "rate_limited")

    def login(self, db, data):
        user = db.scalar(select(m.User).where(m.User.username == data.username))
        if not verify_password(user.password_hash if user else None, data.password):
            raise Problem(401, "invalid_credentials")
        if user.status != "active":
            raise Problem(403, "account_disabled")
        # Reauthentication must not consume quota belonging to expired sessions,
        # even while the PBX supervisor is offline.
        for previous in db.scalars(
            select(m.Device).where(m.Device.user_id == user.id, m.Device.status == "active")
        ):
            valid = db.scalar(
                select(m.AuthSession.id)
                .where(
                    m.AuthSession.device_id == previous.id,
                    m.AuthSession.revoked.is_(False),
                    m.AuthSession.expires_at > time.time(),
                )
                .limit(1)
            )
            if not valid:
                self.revoke(db, previous)
        db.flush()
        count = db.scalar(
            select(func.count())
            .select_from(m.Device)
            .where(m.Device.user_id == user.id, m.Device.status == "active")
        )
        if user.device_limit is not None and count >= user.device_limit:
            raise Problem(409, "device_limit_reached")
        device = m.Device(
            user_id=user.id,
            name=data.device_name,
            platform=data.platform,
            sip_secret=self.vault.encrypt(secrets.token_urlsafe(32)),
        )
        db.add(device)
        db.flush()
        token = secrets.token_urlsafe(48)
        session = m.AuthSession(
            device_id=device.id,
            token_hash=digest(token),
            expires_at=time.time() + self.settings.session_seconds,
        )
        db.add(session)
        db.flush()
        audit(db, user.id, "session_created", device.id)
        return self.session_payload(Identity(user, device, session, token))

    def session_payload(self, ident):
        return dict(
            access_token=ident.token,
            expires_at=utc(ident.session.expires_at),
            user_id=ident.user.id,
            extension=ident.user.extension,
            device_id=ident.device.id,
            account_status=ident.user.status,
            device_limit=ident.user.device_limit,
        )

    def own_device(self, db, ident, device_id, admin=False):
        device = db.get(m.Device, device_id)
        if not device or (device.user_id != ident.user.id and not admin):
            raise Problem(404, "device_not_found")
        return device

    def revoke(self, db, device):
        device.status, device.push_token, device.push_hash, device.sip_expires_at = (
            "revoked",
            None,
            None,
            0,
        )
        for session in db.scalars(
            select(m.AuthSession).where(m.AuthSession.device_id == device.id)
        ):
            session.revoked = True
        for task in db.scalars(
            select(m.OutboundTask).where(
                m.OutboundTask.device_id == device.id,
                m.OutboundTask.state.in_(["waiting", "ringing", "dialing", "connected"]),
            )
        ):
            task.state, task.revision = "cancelled", task.revision + 1
            if task.call_id:
                call = db.get(m.Call, task.call_id)
                change(call, "cancelled")
                command(db, "end_call", call.id)
        for leg in db.scalars(select(m.Leg).where(m.Leg.device_id == device.id)):
            call = db.get(m.Call, leg.call_id)
            if call.state in TERMINAL:
                continue
            leg.state = "cancelled"
            command(db, "hangup_leg", call.id, device.id)
            remaining = db.scalar(
                select(func.count())
                .select_from(m.Leg)
                .where(
                    m.Leg.call_id == call.id,
                    m.Leg.device_id != device.id,
                    m.Leg.role == "callee",
                    m.Leg.state.in_(["waiting", "ringing", "up"]),
                )
            )
            if device.id in {call.origin_device_id, call.winner_device_id} or not remaining:
                change(call, "cancelled")
                command(db, "end_call", call.id)
        audit(db, device.user_id, "device_revoked", device.id)

    def own_call(self, db, ident, call_id):
        leg = db.scalar(
            select(m.Leg).where(m.Leg.call_id == call_id, m.Leg.device_id == ident.device.id)
        )
        if not leg:
            raise Problem(404, "call_not_found")
        return db.get(m.Call, call_id), leg

    def call_payload(self, db, call, leg):
        winner = call.winner_device_id
        remote = call.remote
        if leg.role == "origin":
            remote = db.get(m.User, call.user_id).extension
            winner = call.origin_device_id if winner else None
        return dict(
            id=call.id,
            sip_call_id=leg.sip_call_id or "",
            state=call.state,
            remote=remote,
            updated_at=utc(call.updated_at),
            revision=call.revision,
            answered_device_id=winner,
            queue_position=1 if call.kind == "external" and call.state == "waiting" else None,
            waiting_since=utc(call.created_at) if call.state == "waiting" else None,
        )

    def idempotent(self, db, ident, operation, key, body, action):
        if not key or len(key) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise Problem(400, "idempotency_key_required")
        fingerprint = digest(json.dumps(body, sort_keys=True, separators=(",", ":")))
        row = db.scalar(
            select(m.Idempotency).where(
                m.Idempotency.device_id == ident.device.id,
                m.Idempotency.operation == operation,
                m.Idempotency.key == key,
            )
        )
        if row:
            if row.request_hash != fingerprint:
                raise Problem(409, "idempotency_conflict")
            return json.loads(row.response)
        result = action()
        db.add(
            m.Idempotency(
                device_id=ident.device.id,
                operation=operation,
                key=key,
                request_hash=fingerprint,
                response=json.dumps(result),
            )
        )
        return result

    def call_action(self, db, ident, call_id, action):
        call, leg = self.own_call(db, ident, call_id)
        if action == "claim":
            if call.state in TERMINAL or time.time() >= call.deadline:
                raise Problem(409, "call_expired")
            if call.winner_device_id and call.winner_device_id != ident.device.id:
                raise Problem(409, "answered_elsewhere")
            if leg.role != "callee" or leg.state not in {"ringing", "up"}:
                raise Problem(409, "sip_not_ringing")
            if not call.winner_device_id:
                call.winner_device_id, call.claimed_at = ident.device.id, time.time()
                change(call, "claimed")
                for other in db.scalars(
                    select(m.Leg).where(
                        m.Leg.call_id == call.id,
                        m.Leg.role == "callee",
                        m.Leg.device_id != ident.device.id,
                    )
                ):
                    other.state = "cancelled"
                    command(db, "hangup_leg", call.id, other.device_id)
        elif action == "reject":
            if leg.role != "callee":
                raise Problem(403, "not_callee")
            if call.state not in TERMINAL:
                leg.state = "rejected"
                command(db, "hangup_leg", call.id, ident.device.id)
                others = db.scalar(
                    select(func.count())
                    .select_from(m.Leg)
                    .where(
                        m.Leg.call_id == call.id,
                        m.Leg.role == "callee",
                        m.Leg.device_id != ident.device.id,
                        m.Leg.state.in_(["waiting", "ringing", "up"]),
                    )
                )
                if call.winner_device_id == ident.device.id or not others:
                    change(call, "cancelled")
                    command(db, "end_call", call.id)
        elif action == "hangup":
            if ident.device.id not in {call.origin_device_id, call.winner_device_id}:
                raise Problem(403, "not_call_participant")
            change(call, "ended")
            command(db, "end_call", call.id)
        else:
            raise Problem(404, "action_not_found")
        audit(db, ident.user.id, "call_" + action, call.id)
        return self.call_payload(db, call, leg)

    def task_payload(self, db, task):
        position = None
        if task.state == "waiting":
            ordered = list(
                db.scalars(
                    select(m.OutboundTask.id)
                    .where(m.OutboundTask.state == "waiting")
                    .order_by(
                        m.OutboundTask.priority.desc(), m.OutboundTask.created_at, m.OutboundTask.id
                    )
                )
            )
            position = ordered.index(task.id) + 1 if task.id in ordered else None
        return dict(
            id=task.id,
            destination=task.destination,
            state=task.state,
            revision=task.revision,
            position=position,
            estimated_wait_seconds=None,
            failure_reason=task.failure_reason,
        )

    def connection_status(self, db, ident, ready):
        lease = db.get(m.LineLease, "fxo")
        return dict(
            account_status=ident.user.status,
            device_status=ident.device.status,
            line_status=lease.state if ready and self.settings.fxo_enabled and lease else "offline",
            can_call=ready,
        )

    def status(self, db, ident, ready):
        # Same participant scope as snapshot, but aggregate every row without its display limit.
        calls = (
            select(func.count(m.Call.id))
            .join(m.Leg, m.Leg.call_id == m.Call.id)
            .where(m.Leg.device_id == ident.device.id)
        )
        return dict(
            **self.connection_status(db, ident, ready),
            server_status="ready" if ready else "degraded",
            pbx_ready=ready,
            fxo_enabled=self.settings.fxo_enabled,
            extension=ident.user.extension,
            active_calls=db.scalar(calls.where(m.Call.state.not_in(TERMINAL))),
            waiting_inbound_calls=db.scalar(
                calls.where(m.Call.kind == "external", m.Call.state == "waiting")
            ),
            waiting_outbound_tasks=db.scalar(
                select(func.count(m.OutboundTask.id)).where(
                    m.OutboundTask.user_id == ident.user.id, m.OutboundTask.state == "waiting"
                )
            ),
            observed_at=utc(time.time()),
        )

    def snapshot(self, db, ident, ready):
        ident.device.last_seen_at = time.time()
        devices = [
            dict(id=d.id, name=d.name, status=d.status, last_seen_at=utc(d.last_seen_at))
            for d in db.scalars(
                select(m.Device)
                .where(m.Device.user_id == ident.user.id)
                .order_by(m.Device.last_seen_at.desc())
            )
        ]
        calls, history = [], []
        rows = db.execute(
            select(m.Call, m.Leg)
            .join(m.Leg, m.Call.id == m.Leg.call_id)
            .where(m.Leg.device_id == ident.device.id)
            .order_by(m.Call.created_at.desc())
            .limit(200)
        )
        for call, leg in rows:
            (history if call.state in TERMINAL else calls).append(self.call_payload(db, call, leg))
        tasks = [
            self.task_payload(db, t)
            for t in db.scalars(
                select(m.OutboundTask)
                .where(m.OutboundTask.user_id == ident.user.id)
                .order_by(m.OutboundTask.created_at.desc())
                .limit(200)
            )
        ]
        return dict(
            **self.connection_status(db, ident, ready),
            devices=devices,
            calls=calls,
            history=history,
            outbound_tasks=tasks,
        )
