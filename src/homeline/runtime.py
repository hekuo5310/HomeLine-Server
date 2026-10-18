# SPDX-License-Identifier: MPL-2.0
"""Single-process durable control plane. PBX events alone establish connected calls."""

import asyncio
import fcntl
import logging
import time

from sqlalchemy import delete, select

from . import models as m
from .asterisk import Asterisk, PBXError
from .provisioning import atomic_write, render_devices
from .push import FirebasePush, PushFailure
from .service import TERMINAL, Problem, audit, change, command, utc

log = logging.getLogger("homeline.runtime")


class Runtime:
    def __init__(self, service, pbx=None, push=None):
        self.service = service
        self.database, self.settings = service.database, service.settings
        self.pbx = pbx if pbx is not None else Asterisk(self.settings)
        self.push = push if push is not None else FirebasePush(self.settings)
        self.ready = False
        self.connected = False
        self.tasks = []
        self.actions = asyncio.Lock()
        self.provision_lock = asyncio.Lock()
        self.rendered = None
        self.lock = None

    async def start(self):
        if not self.settings.worker_enabled:
            return
        self.settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.settings.lock_file.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError("Only one HomeLine worker may control Asterisk") from None
        with self.database.transaction() as db:
            if not db.get(m.LineLease, "fxo"):
                db.add(m.LineLease(line_id="fxo"))
        self.tasks = [
            asyncio.create_task(self.listen()),
            asyncio.create_task(self.run()),
            asyncio.create_task(self.push_loop()),
        ]

    async def stop(self):
        self.ready = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.pbx.close()
        await self.push.close()
        if self.lock:
            self.lock.close()
            self.lock = None

    async def listen(self):
        while True:
            try:
                async for event in self.pbx.events():
                    if event["type"] == "HomeLineConnected":
                        self.connected = True
                        self.ready = False
                    else:
                        async with self.actions:
                            if self.ready:
                                await self.handle(event)
                            elif event.get("type") == "StasisStart":
                                await self.pbx.hangup(event["channel"]["id"], "congestion")
                self.connected = False
                self.ready = False
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected = False
                self.ready = False
                log.warning("ARI event stream unavailable; control paused")
            await asyncio.sleep(1)

    async def run(self):
        while True:
            try:
                if self.connected:
                    async with self.actions:
                        if not self.ready:
                            await self.recover()
                        self.expire()
                        await self.sync_call_ids()
                        await self.schedule()
                        for _ in range(8):
                            if not await self.execute_next():
                                break
                    await self.provision()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ready = False
                log.warning("PBX control unavailable; recovery required")
            await asyncio.sleep(0.25)

    async def provision(self):
        if not self.connected and self.settings.worker_enabled:
            raise Problem(503, "pbx_unavailable")
        async with self.provision_lock:
            data = render_devices(self.service)
            if data == self.rendered:
                return
            atomic_write(self.settings.pjsip_file, data)
            try:
                await self.pbx.reload()
            except PBXError:
                self.ready = False
                raise Problem(503, "sip_provision_failed") from None
            self.rendered = data

    def foreign_fxo(self, channels, excluding=None):
        return any(
            c["id"] != excluding
            and c.get("name", "").startswith("PJSIP/" + self.settings.fxo_endpoint + "-")
            for c in channels
        )

    async def recover(self):
        await self.pbx.info()
        actual = await self.pbx.channels()
        with self.database.transaction(False) as db:
            calls = list(db.scalars(select(m.Call).where(m.Call.state.not_in(TERMINAL))))
            # Include terminal calls awaiting durable cleanup and all known channels.
            owned = {x for x in db.scalars(select(m.Leg.channel_id)) if x}
            owned.update(x for x in db.scalars(select(m.Call.source_channel)) if x)
            owned.update(x for x in db.scalars(select(m.LineLease.channel_id)) if x)
            bridges = {x for x in db.scalars(select(m.Call.bridge_id)) if x}
        owned.update(c["id"] for c in actual if c["id"].startswith("hl-"))
        for channel in actual:
            if channel["id"] in owned:
                await self.pbx.hangup(channel["id"])
        for bridge in bridges:
            await self.pbx.destroy_bridge(bridge)
        actual = await self.pbx.channels()
        if any(c["id"] in owned for c in actual):
            raise PBXError("owned_channels_not_cleared")
        with self.database.transaction() as db:
            for old in calls:
                call = db.get(m.Call, old.id)
                change(call, "failed")
                call.failure_reason = "pbx_control_restarted"
            for leg in db.scalars(
                select(m.Leg).where(m.Leg.state.in_(["waiting", "ringing", "up"]))
            ):
                leg.state = "ended"
            for task in db.scalars(
                select(m.OutboundTask).where(
                    m.OutboundTask.state.in_(["ringing", "dialing", "connected"])
                )
            ):
                task.state = "failed"
                task.failure_reason = "pbx_control_restarted"
                task.revision += 1
            for item in db.scalars(select(m.Outbox).where(m.Outbox.status == "running")):
                item.status = "pending"
            lease = db.get(m.LineLease, "fxo")
            if not lease:
                lease = m.LineLease(line_id="fxo")
                db.add(lease)
            lease.call_id = None
            lease.channel_id = None
            lease.state = "busy" if self.foreign_fxo(actual) else "idle"
            lease.updated_at = time.time()
            audit(db, None, "pbx_reconciled")
        self.ready = True

    def eligible(self, db, user_id):
        now = time.time()
        return list(
            db.scalars(
                select(m.Device)
                .join(m.User, m.User.id == m.Device.user_id)
                .where(
                    m.Device.user_id == user_id,
                    m.Device.status == "active",
                    m.User.status == "active",
                    select(m.AuthSession.id)
                    .where(
                        m.AuthSession.device_id == m.Device.id,
                        m.AuthSession.revoked.is_(False),
                        m.AuthSession.expires_at > now,
                    )
                    .exists(),
                )
            )
        )

    def offer(self, db, user, remote, kind, origin=None, source=None):
        call = m.Call(
            user_id=user.id,
            remote=remote,
            kind=kind,
            origin_device_id=origin,
            source_channel=source,
            deadline=time.time() + self.settings.ring_seconds,
        )
        db.add(call)
        db.flush()
        count = 0
        for device in self.eligible(db, user.id):
            if device.id == origin:
                continue
            count += 1
            db.add(m.Leg(call_id=call.id, device_id=device.id))
            command(db, "ring_leg", call.id, device.id)
            if device.push_token:
                command(db, "push", call.id, device.id)
        if not count:
            change(call, "failed")
            call.failure_reason = "no_eligible_device"
            command(db, "end_call", call.id)
        audit(db, None, "call_offered", call.id)
        return call

    async def schedule(self):
        if not self.ready or not self.settings.fxo_enabled:
            return
        actual = await self.pbx.channels()
        with self.database.transaction() as db:
            lease = db.get(m.LineLease, "fxo")
            if not lease or lease.call_id:
                return
            lease.state = "busy" if self.foreign_fxo(actual) else "idle"
            if lease.state != "idle":
                return
            tasks = db.scalars(
                select(m.OutboundTask)
                .where(m.OutboundTask.state == "waiting")
                .order_by(
                    m.OutboundTask.priority.desc(), m.OutboundTask.created_at, m.OutboundTask.id
                )
            )
            for task in tasks:
                device = db.get(m.Device, task.device_id)
                user = db.get(m.User, task.user_id)
                if device.status != "active" or user.status != "active":
                    task.state = "cancelled"
                    task.revision += 1
                    continue
                call = self.offer(db, user, task.destination, "outbound")
                task.call_id = call.id
                task.state = "ringing"
                task.revision += 1
                lease.call_id = call.id
                lease.generation += 1
                lease.state = "busy"
                lease.updated_at = time.time()
                break

    async def handle(self, event):
        kind = event.get("type")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        if kind == "StasisStart":
            args = event.get("args", [])
            if args and args[0] in {"internal", "external"}:
                await self.incoming(channel, args)
            elif channel_id:
                await self.record_wire(channel)
                if channel.get("state") == "Up":
                    await self.up(channel_id)
        elif kind == "ChannelStateChange" and channel_id:
            await self.record_wire(channel)
            if channel.get("state") == "Up":
                await self.up(channel_id)
            elif channel.get("state") == "Ringing":
                with self.database.transaction() as db:
                    lease = db.scalar(
                        select(m.LineLease).where(m.LineLease.channel_id == channel_id)
                    )
                    if lease and lease.call_id:
                        task = db.scalar(
                            select(m.OutboundTask).where(m.OutboundTask.call_id == lease.call_id)
                        )
                        if task and task.state == "dialing":
                            task.state = "ringing"
                            task.revision += 1
        elif kind in {"ChannelDestroyed", "StasisEnd"} and channel_id:
            self.down(channel_id)

    async def incoming(self, channel, args):
        channel_id = channel["id"]
        endpoint = await self.pbx.variable(channel_id, "CHANNEL(endpoint)")
        secure = (
            await self.pbx.variable(channel_id, "CHANNEL(pjsip,secure)")
            if args[0] == "internal"
            else ""
        )
        wire = await self.pbx.wire_id(channel)
        actual = await self.pbx.channels() if args[0] == "external" else []
        accepted = False
        with self.database.transaction() as db:
            existing = db.scalar(select(m.Call).where(m.Call.source_channel == channel_id))
            if existing:
                return
            if args[0] == "internal" and len(args) == 3:
                device = db.get(m.Device, args[1])
                target = db.scalar(
                    select(m.User).where(m.User.extension == args[2], m.User.status == "active")
                )
                user = db.get(m.User, device.user_id) if device else None
                if (
                    device
                    and target
                    and user
                    and user.status == "active"
                    and device.status == "active"
                    and device.sip_expires_at > time.time()
                    and endpoint == "d" + device.id
                    and secure == "1"
                    and target.id != user.id
                ):
                    call = self.offer(db, target, user.extension, "internal", device.id, channel_id)
                    db.add(
                        m.Leg(
                            call_id=call.id,
                            device_id=device.id,
                            channel_id=channel_id,
                            sip_call_id=wire or None,
                            role="origin",
                            state="up",
                        )
                    )
                    accepted = call.state not in TERMINAL
            elif (
                args[0] == "external"
                and self.settings.fxo_enabled
                and endpoint == self.settings.fxo_endpoint
            ):
                user = db.scalar(
                    select(m.User).where(
                        m.User.extension == self.settings.incoming_extension,
                        m.User.status == "active",
                    )
                )
                lease = db.get(m.LineLease, "fxo")
                if (
                    user
                    and lease
                    and not lease.call_id
                    and not self.foreign_fxo(actual, channel_id)
                ):
                    remote = "".join(
                        c
                        for c in channel.get("caller", {}).get("number", "unknown")[:80]
                        if c.isalnum() or c in "+ -"
                    )
                    call = self.offer(db, user, remote, "external", source=channel_id)
                    lease.call_id = call.id
                    lease.channel_id = channel_id
                    lease.state = "busy"
                    lease.generation += 1
                    accepted = call.state not in TERMINAL
        if accepted:
            await self.pbx.ring(channel_id)
        else:
            await self.pbx.hangup(channel_id, "rejected")

    async def record_wire(self, channel):
        with self.database.transaction(False) as db:
            exists = db.scalar(
                select(m.Leg.id).where(
                    m.Leg.channel_id == channel["id"], m.Leg.sip_call_id.is_(None)
                )
            )
        if not exists:
            return
        wire = await self.pbx.wire_id(channel)
        if not wire:
            return
        with self.database.transaction() as db:
            leg = db.get(m.Leg, exists)
            if not leg.sip_call_id:
                leg.sip_call_id = wire
                call = db.get(m.Call, leg.call_id)
                call.revision += 1
                call.updated_at = time.time()
                audit(db, None, "sip_call_identified", call.id)

    async def sync_call_ids(self):
        with self.database.transaction(False) as db:
            missing = list(
                db.scalars(
                    select(m.Leg.channel_id)
                    .join(m.Call, m.Call.id == m.Leg.call_id)
                    .where(
                        m.Leg.sip_call_id.is_(None),
                        m.Leg.channel_id.is_not(None),
                        m.Call.state.not_in(TERMINAL),
                    )
                )
            )
        if missing:
            for channel in await self.pbx.channels():
                if channel["id"] in missing:
                    await self.record_wire(channel)

    async def up(self, channel_id):
        action = None
        call_id = None
        with self.database.transaction() as db:
            leg = db.scalar(select(m.Leg).where(m.Leg.channel_id == channel_id))
            lease = db.scalar(select(m.LineLease).where(m.LineLease.channel_id == channel_id))
            if leg:
                call = db.get(m.Call, leg.call_id)
                call_id = call.id
                if (
                    call.state in TERMINAL
                    or leg.state in {"cancelled", "rejected", "ended", "failed"}
                    or (leg.role == "callee" and call.winner_device_id != leg.device_id)
                ):
                    command(db, "hangup_leg", call.id, leg.device_id)
                    return
                leg.state = "up"
                if leg.role == "origin":
                    return
                call.claimed_at = None
                action = "trunk" if call.kind == "outbound" else "connect"
            elif lease and lease.call_id:
                call_id = lease.call_id
                if db.get(m.Call, call_id).state not in TERMINAL:
                    action = "connect"
        if action == "trunk":
            await self.dial_trunk(call_id)
        elif action == "connect":
            await self.connect(call_id)

    async def dial_trunk(self, call_id):
        with self.database.transaction() as db:
            call = db.get(m.Call, call_id)
            lease = db.get(m.LineLease, "fxo")
            if call.state in TERMINAL or lease.call_id != call.id or lease.channel_id:
                return
            channel = "hl-" + call.id + "-fxo"
            lease.channel_id = channel
            task = db.scalar(select(m.OutboundTask).where(m.OutboundTask.call_id == call.id))
            task.state = "dialing"
            task.revision += 1
            destination = task.destination
            call.deadline = time.time() + self.settings.ring_seconds
            audit(db, None, "external_dial_started", call.id)
        try:
            await self.pbx.create(
                channel,
                "PJSIP/" + destination + "@" + self.settings.fxo_endpoint,
                "trunk," + call_id,
                destination,
            )
            await self.pbx.dial(channel, self.settings.ring_seconds)
        except Exception:
            with self.database.transaction() as db:
                call = db.get(m.Call, call_id)
                change(call, "failed")
                call.failure_reason = "external_dial_uncertain"
                command(db, "end_call", call_id)
            raise

    async def connect(self, call_id):
        with self.database.transaction(False) as db:
            call = db.get(m.Call, call_id)
            if call.state in TERMINAL or call.state == "connected":
                return
            winner = db.scalar(
                select(m.Leg).where(
                    m.Leg.call_id == call.id,
                    m.Leg.device_id == call.winner_device_id,
                    m.Leg.state == "up",
                )
            )
            if not winner:
                return
            source = call.source_channel
            if call.kind == "outbound":
                lease = db.get(m.LineLease, "fxo")
                source = lease.channel_id if lease.call_id == call.id else None
            if not source:
                return
            destination = winner.channel_id
            inbound = call.kind != "outbound"
        if inbound:
            await self.pbx.answer(source)
        bridge = "hl-" + call_id
        await self.pbx.bridge(bridge, [source, destination])
        with self.database.transaction() as db:
            call = db.get(m.Call, call_id)
            call.bridge_id = bridge
            if call.state in TERMINAL:
                command(db, "end_call", call_id)
            else:
                change(call, "connected")
                task = db.scalar(select(m.OutboundTask).where(m.OutboundTask.call_id == call_id))
                if task:
                    task.state = "connected"
                    task.revision += 1
                audit(db, None, "call_connected", call_id)

    def down(self, channel_id):
        with self.database.transaction() as db:
            leg = db.scalar(select(m.Leg).where(m.Leg.channel_id == channel_id))
            call = (
                db.get(m.Call, leg.call_id)
                if leg
                else db.scalar(select(m.Call).where(m.Call.source_channel == channel_id))
            )
            if not call:
                lease = db.scalar(select(m.LineLease).where(m.LineLease.channel_id == channel_id))
                if lease and lease.call_id:
                    call = db.get(m.Call, lease.call_id)
            if not call or call.state in TERMINAL:
                return
            if leg:
                leg.state = "ended"
            remaining = db.scalar(
                select(m.Leg.id)
                .where(
                    m.Leg.call_id == call.id,
                    m.Leg.role == "callee",
                    m.Leg.channel_id != channel_id,
                    m.Leg.state.in_(["waiting", "ringing", "up"]),
                )
                .limit(1)
            )
            # NULL channel IDs are still eligible waiting legs.
            if not remaining:
                remaining = db.scalar(
                    select(m.Leg.id)
                    .where(
                        m.Leg.call_id == call.id,
                        m.Leg.role == "callee",
                        m.Leg.channel_id.is_(None),
                        m.Leg.state == "waiting",
                    )
                    .limit(1)
                )
            if (
                not leg
                or leg.role == "origin"
                or call.winner_device_id == leg.device_id
                or not remaining
            ):
                connected = call.state == "connected"
                change(call, "ended" if connected else "failed")
                if not connected:
                    call.failure_reason = "sip_leg_ended"
                command(db, "end_call", call.id)
                audit(db, None, "call_ended", call.id)

    def expire(self):
        now = time.time()
        with self.database.transaction() as db:
            for call in db.scalars(
                select(m.Call).where(m.Call.state.not_in(TERMINAL), m.Call.state != "connected")
            ):
                if call.deadline <= now or (
                    call.claimed_at and call.claimed_at + self.settings.claim_seconds <= now
                ):
                    change(call, "timeout")
                    call.failure_reason = "no_answer"
                    command(db, "end_call", call.id)
                    audit(db, None, "call_timeout", call.id)
            for device in db.scalars(select(m.Device).where(m.Device.status == "active")):
                active = db.scalar(
                    select(m.AuthSession.id)
                    .where(
                        m.AuthSession.device_id == device.id,
                        m.AuthSession.revoked.is_(False),
                        m.AuthSession.expires_at > now,
                    )
                    .limit(1)
                )
                if not active:
                    self.service.revoke(db, device)
            db.execute(delete(m.RateBucket).where(m.RateBucket.start < now - 3600))

    async def execute_next(self):
        with self.database.transaction() as db:
            item = db.scalar(
                select(m.Outbox)
                .where(
                    m.Outbox.status == "pending",
                    m.Outbox.kind != "push",
                    m.Outbox.available_at <= time.time(),
                )
                .order_by(m.Outbox.available_at, m.Outbox.id)
                .limit(1)
            )
            if not item:
                return False
            item.status = "running"
            item.attempts += 1
            item_id, kind, call_id, device_id, attempts = (
                item.id,
                item.kind,
                item.call_id,
                item.device_id,
                item.attempts,
            )
        try:
            if kind == "ring_leg":
                await self.ring_leg(call_id, device_id)
            elif kind == "hangup_leg":
                with self.database.transaction(False) as db:
                    channel = db.scalar(
                        select(m.Leg.channel_id).where(
                            m.Leg.call_id == call_id, m.Leg.device_id == device_id
                        )
                    )
                if channel:
                    await self.pbx.hangup(channel)
            elif kind == "end_call":
                await self.end_call(call_id)
            else:
                raise ValueError("unknown command")
        except PBXError:
            with self.database.transaction() as db:
                item = db.get(m.Outbox, item_id)
                item.status = "pending"
                item.last_error = "pbx_unavailable"
                item.available_at = time.time() + min(8, 2 ** min(attempts, 3))
            return False
        with self.database.transaction() as db:
            db.get(m.Outbox, item_id).status = "done"
        return True

    async def ring_leg(self, call_id, device_id):
        with self.database.transaction() as db:
            call = db.get(m.Call, call_id)
            device = db.get(m.Device, device_id)
            leg = db.scalar(
                select(m.Leg).where(m.Leg.call_id == call_id, m.Leg.device_id == device_id)
            )
            if (
                call.state in TERMINAL
                or leg.state != "waiting"
                or call.winner_device_id
                or device.status != "active"
            ):
                return
            if device.sip_expires_at <= time.time():
                raise PBXError("waiting_for_device_registration")
            channel = leg.channel_id or "hl-" + call_id + "-" + device_id
            leg.channel_id = channel
            caller = call.remote if call.kind != "outbound" else "HomeLine"
            deadline = call.deadline
        created = await self.pbx.create(channel, "PJSIP/d" + device_id, "leg," + call_id, caller)
        wire = await self.pbx.wire_id(created)
        if not wire:
            raise PBXError("sip_call_id_unavailable")
        with self.database.transaction() as db:
            call = db.get(m.Call, call_id)
            leg = db.scalar(
                select(m.Leg).where(m.Leg.call_id == call_id, m.Leg.device_id == device_id)
            )
            cancelled = call.state in TERMINAL or call.winner_device_id or leg.state != "waiting"
            if not cancelled:
                leg.sip_call_id = wire
                leg.state = "ringing"
                change(call, "ringing")
                audit(db, None, "device_ringing", call_id)
        if cancelled:
            await self.pbx.hangup(channel)
            return
        try:
            await self.pbx.dial(channel, max(1, int(deadline - time.time())))
        except Exception:
            # Uncertain INVITE is not retried; durable cleanup retains known channel ID.
            with self.database.transaction() as db:
                leg = db.scalar(
                    select(m.Leg).where(m.Leg.call_id == call_id, m.Leg.device_id == device_id)
                )
                leg.state = "failed"
                command(db, "hangup_leg", call_id, device_id)
            raise

    async def end_call(self, call_id):
        with self.database.transaction(False) as db:
            call = db.get(m.Call, call_id)
            channels = {
                c for c in db.scalars(select(m.Leg.channel_id).where(m.Leg.call_id == call_id)) if c
            }
            if call.source_channel:
                channels.add(call.source_channel)
            lease = db.get(m.LineLease, "fxo")
            if lease.call_id == call_id and lease.channel_id:
                channels.add(lease.channel_id)
            bridge = call.bridge_id
        for channel in channels:
            await self.pbx.hangup(channel)
        if bridge:
            await self.pbx.destroy_bridge(bridge)
        actual = await self.pbx.channels()
        if any(c["id"] in channels for c in actual):
            raise PBXError("channels_still_present")
        with self.database.transaction() as db:
            call = db.get(m.Call, call_id)
            for leg in db.scalars(select(m.Leg).where(m.Leg.call_id == call_id)):
                leg.state = "ended"
            task = db.scalar(select(m.OutboundTask).where(m.OutboundTask.call_id == call_id))
            if task and task.state not in {"completed", "cancelled", "failed"}:
                task.state = (
                    "completed"
                    if call.state == "ended"
                    else ("cancelled" if call.state == "cancelled" else "failed")
                )
                task.failure_reason = call.failure_reason
                task.revision += 1
            lease = db.get(m.LineLease, "fxo")
            if lease.call_id == call_id:
                lease.call_id = None
                lease.channel_id = None
                lease.updated_at = time.time()
                lease.state = "busy" if self.foreign_fxo(actual) else "idle"
                audit(db, None, "line_released", call_id)

    async def push_loop(self):
        while True:
            try:
                if not await self.push_next():
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Push worker failed; retrying pending deliveries")
                await asyncio.sleep(1)

    async def push_next(self):
        with self.database.transaction() as db:
            item = db.scalar(
                select(m.Outbox)
                .where(
                    m.Outbox.kind == "push",
                    m.Outbox.status == "pending",
                    m.Outbox.available_at <= time.time(),
                )
                .order_by(m.Outbox.available_at)
                .limit(1)
            )
            if not item:
                return False
            call = db.get(m.Call, item.call_id)
            device = db.get(m.Device, item.device_id)
            if (
                call.state in TERMINAL
                or call.deadline <= time.time()
                or not device.push_token
                or device.status != "active"
            ):
                item.status = "done"
                return True
            item.status = "running"
            item.attempts += 1
            item_id, device_id, token_hash, call_id, deadline, attempts = (
                item.id,
                device.id,
                device.push_hash,
                call.id,
                call.deadline,
                item.attempts,
            )
            token = self.service.vault.decrypt(device.push_token)
        try:
            await self.push.send(
                token,
                {
                    "type": "incoming_call",
                    "call_id": call_id,
                    "expires_at": utc(min(deadline, time.time() + 120)),
                },
                min(120, int(deadline - time.time())),
            )
        except PushFailure as exc:
            with self.database.transaction() as db:
                item = db.get(m.Outbox, item_id)
                item.status = "failed" if exc.permanent or time.time() >= deadline else "pending"
                item.last_error = str(exc)
                item.available_at = time.time() + min(15, 2 ** min(attempts, 4))
                device = db.get(m.Device, device_id)
                if exc.invalid_token and device.push_hash == token_hash:
                    device.push_token = None
                    device.push_hash = None
        else:
            with self.database.transaction() as db:
                db.get(m.Outbox, item_id).status = "done"
                audit(db, None, "fcm_accepted", call_id)
        return True
