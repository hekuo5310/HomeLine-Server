# SPDX-License-Identifier: MPL-2.0
import asyncio
import time

import pytest
from sqlalchemy import func, select
from test_api import auth, login

from homeline import models as m
from homeline.asterisk import PBXError
from homeline.push import PushFailure
from homeline.runtime import Runtime
from homeline.service import change, command


class PBX:
    def __init__(self):
        self.actual = {}
        self.dials = []
        self.hangups = []
        self.bridges = []
        self.leave_channels = False
        self.fail_dial = False

    async def info(self):
        return {}

    async def list_bridges(self):
        return []

    async def channels(self):
        return list(self.actual.values())

    async def create(self, channel, endpoint, args, caller):
        result = {
            "id": channel,
            "protocol_id": "real-test-wire-" + channel,
            "name": endpoint + "-00001",
        }
        self.actual[channel] = result
        return result

    async def wire_id(self, channel):
        return channel["protocol_id"]

    async def dial(self, channel, timeout):
        self.dials.append(channel)
        if self.fail_dial:
            raise PBXError("uncertain")

    async def hangup(self, channel, reason="normal"):
        self.hangups.append(channel)
        if not self.leave_channels:
            self.actual.pop(channel, None)

    async def answer(self, channel):
        pass

    async def bridge(self, bridge, channels):
        self.bridges.append(channels)

    async def destroy_bridge(self, bridge):
        pass

    async def reload(self):
        pass

    async def close(self):
        pass


class Push:
    def __init__(self):
        self.invalid = False
        self.sent = []
        self.during = None

    async def send(self, token, data, ttl):
        self.sent.append(data)
        if self.during:
            self.during()
        if self.invalid:
            raise PushFailure("invalid_token", True, True)

    async def close(self):
        pass


def runtime(app):
    rt = Runtime(app.state.service, PBX(), Push())
    rt.ready = True
    rt.connected = True
    with rt.database.transaction() as db:
        db.add(m.LineLease(line_id="fxo", state="idle"))
    return rt


def eligible_device(rt, session):
    with rt.database.transaction() as db:
        db.get(m.Device, session["device_id"]).sip_expires_at = time.time() + 600


def offer(rt, session, kind="external"):
    with rt.database.transaction() as db:
        call = rt.offer(
            db,
            db.get(m.User, session["user_id"]),
            "123456",
            kind,
            source="incoming-source" if kind != "outbound" else None,
        )
        return call.id


def test_real_events_required_and_loser_cancelled(app):
    a, c = app
    one = login(c)
    two = login(c, name="second")
    rt = runtime(a)
    for s in [one, two]:
        eligible_device(rt, s)
    call = offer(rt, one)

    async def run():
        await rt.ring_leg(call, one["device_id"])
        await rt.ring_leg(call, two["device_id"])
        assert len(rt.pbx.dials) == 2
        assert c.post(f"/v1/calls/{call}/claim", headers=auth(one)).status_code == 200
        with rt.database.transaction(False) as db:
            assert db.get(m.Call, call).state == "claimed"
            winner = db.scalar(
                select(m.Leg).where(m.Leg.call_id == call, m.Leg.device_id == one["device_id"])
            )
            loser = db.scalar(
                select(m.Leg).where(m.Leg.call_id == call, m.Leg.device_id == two["device_id"])
            )
            assert loser.state == "cancelled"
        await rt.up(loser.channel_id)
        assert not rt.pbx.bridges
        await rt.up(winner.channel_id)
        assert rt.pbx.bridges
        with rt.database.transaction(False) as db:
            assert db.get(m.Call, call).state == "connected"
        rt.down(loser.channel_id)
        with rt.database.transaction(False) as db:
            assert db.get(m.Call, call).state == "connected"

    asyncio.run(run())


def test_single_line_fifo_and_restart_no_redial(app):
    a, c = app
    session = login(c)
    rt = runtime(a)
    eligible_device(rt, session)
    for i in range(3):
        assert (
            c.post(
                "/v1/outbound-tasks",
                headers=auth(session, str(i)),
                json={"destination": "12345" + str(i)},
            ).status_code
            == 201
        )

    async def run():
        await rt.schedule()
        await rt.schedule()
        with rt.database.transaction(False) as db:
            assert db.scalar(select(func.count()).select_from(m.Call)) == 1
            lease = db.get(m.LineLease, "fxo")
            call_id = lease.call_id
            task = db.scalar(select(m.OutboundTask).where(m.OutboundTask.call_id == call_id))
            assert task.destination == "123450"
        await rt.ring_leg(call_id, session["device_id"])
        assert c.post(f"/v1/calls/{call_id}/claim", headers=auth(session)).status_code == 200
        with rt.database.transaction(False) as db:
            channel = db.scalar(select(m.Leg.channel_id).where(m.Leg.call_id == call_id))
        await rt.up(channel)
        await rt.up(channel)
        assert len([x for x in rt.pbx.dials if x.endswith("-fxo")]) == 1
        await rt.recover()
        assert len([x for x in rt.pbx.dials if x.endswith("-fxo")]) == 1
        with rt.database.transaction(False) as db:
            assert db.get(m.Call, call_id).failure_reason == "pbx_control_restarted"
            assert db.get(m.LineLease, "fxo").call_id is None
        await rt.schedule()
        with rt.database.transaction(False) as db:
            assert db.scalar(select(func.count()).select_from(m.Call)) == 2

    asyncio.run(run())


def test_lease_not_released_until_actual_channels_absent(app):
    a, c = app
    session = login(c)
    rt = runtime(a)
    call = offer(rt, session)
    with rt.database.transaction() as db:
        db.get(m.LineLease, "fxo").call_id = call
        change(db.get(m.Call, call), "failed")
    rt.pbx.actual["incoming-source"] = {"id": "incoming-source"}
    rt.pbx.leave_channels = True
    with pytest.raises(PBXError):
        asyncio.run(rt.end_call(call))
    with rt.database.transaction(False) as db:
        assert db.get(m.LineLease, "fxo").call_id == call
    rt.pbx.leave_channels = False
    asyncio.run(rt.end_call(call))
    with rt.database.transaction(False) as db:
        assert db.get(m.LineLease, "fxo").call_id is None


def test_fcm_invalid_token_refresh_race_and_receipts(app):
    a, c = app
    session = login(c)
    rt = runtime(a)
    c.put(
        "/v1/devices/current/push-token",
        headers=auth(session),
        json={"provider": "fcm", "token": "old"},
    )
    call = offer(rt, session)
    rt.push.invalid = True

    def refresh():
        c.put(
            "/v1/devices/current/push-token",
            headers=auth(session),
            json={"provider": "fcm", "token": "new"},
        )

    rt.push.during = refresh
    asyncio.run(rt.push_next())
    with rt.database.transaction(False) as db:
        assert rt.service.vault.decrypt(db.get(m.Device, session["device_id"]).push_token) == "new"
        assert db.get(m.Call, call).state == "waiting"
    rt.push.during = None
    with rt.database.transaction() as db:
        command(db, "push", call, session["device_id"])
    asyncio.run(rt.push_next())
    with rt.database.transaction(False) as db:
        assert db.get(m.Device, session["device_id"]).push_token is None


def test_uncertain_external_dial_never_replayed(app):
    a, c = app
    session = login(c)
    rt = runtime(a)
    c.post("/v1/outbound-tasks", headers=auth(session), json={"destination": "123456"})

    async def run():
        await rt.schedule()
        with rt.database.transaction(False) as db:
            call = db.get(m.LineLease, "fxo").call_id
        rt.pbx.fail_dial = True
        with pytest.raises(PBXError):
            await rt.dial_trunk(call)
        await rt.dial_trunk(call)
        assert len(rt.pbx.dials) == 1
        with rt.database.transaction(False) as db:
            assert db.get(m.Call, call).failure_reason == "external_dial_uncertain"

    asyncio.run(run())


def test_expiry_and_provision_omit_revoked_endpoint(app):
    a, c = app
    session = login(c)
    rt = runtime(a)
    eligible_device(rt, session)
    asyncio.run(rt.provision())
    assert "d" + session["device_id"] in rt.settings.pjsip_file.read_text()
    with rt.database.transaction() as db:
        db.scalar(
            select(m.AuthSession).where(m.AuthSession.device_id == session["device_id"])
        ).expires_at = time.time() - 1
    rt.expire()
    asyncio.run(rt.provision())
    assert "d" + session["device_id"] not in rt.settings.pjsip_file.read_text()
    assert c.get("/v1/session", headers=auth(session)).status_code == 401


def test_busy_device_is_not_given_second_invite(app):
    a, c = app
    session = login(c)
    rt = runtime(a)
    eligible_device(rt, session)
    one = offer(rt, session, "outbound")
    two = offer(rt, session, "outbound")

    async def run():
        await rt.ring_leg(one, session["device_id"])
        with pytest.raises(PBXError, match="device_busy"):
            await rt.ring_leg(two, session["device_id"])
        assert len(rt.pbx.dials) == 1

    asyncio.run(run())
