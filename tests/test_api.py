# SPDX-License-Identifier: MPL-2.0
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_foundation import migrated

from homeline import models as m
from homeline.api import create_app
from homeline.config import Settings
from homeline.security import hasher


class TestRuntime:
    __test__ = False
    ready = True

    async def start(self):
        pass

    async def stop(self):
        pass

    async def provision(self):
        pass

    async def sync_call_ids(self):
        pass


@pytest.fixture
def app(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    settings = Settings(
        database=tmp_path / "state.sqlite3",
        key_file=key,
        ari_password="test-only",
        sip_domain="localhost",
        sip_wss_url="wss://localhost/ws",
        trusted_hosts=["testserver"],
        fxo_enabled=True,
        pjsip_file=tmp_path / "devices.conf",
        lock_file=tmp_path / "worker.lock",
    )
    migrated(settings.database)
    app = create_app(settings, TestRuntime())
    with app.state.service.database.transaction() as db:
        for name, ext, role in [("alice", "1001", "admin"), ("bob", "1002", "user")]:
            db.add(
                m.User(
                    username=name,
                    extension=ext,
                    role=role,
                    password_hash=hasher.hash("test-only-password"),
                )
            )
    with TestClient(app) as client:
        yield app, client


def login(client, user="alice", name="phone"):
    result = client.post(
        "/v1/sessions",
        json=dict(
            username=user, password="test-only-password", device_name=name, platform="android"
        ),
    )
    assert result.status_code == 201, result.text
    return result.json()


def auth(session, key="test-key"):
    return {"Authorization": "Bearer " + session["access_token"], "Idempotency-Key": key}


def seed_call(app, sessions, kind="external"):
    with app.state.service.database.transaction() as db:
        call = m.Call(
            user_id=sessions[0]["user_id"], kind=kind, remote="123456", deadline=time.time() + 45
        )
        db.add(call)
        db.flush()
        for session in sessions:
            db.add(
                m.Leg(
                    call_id=call.id,
                    device_id=session["device_id"],
                    state="ringing",
                    sip_call_id="wire-" + session["device_id"],
                )
            )
        return call.id


def test_login_credentials_and_sensitive_validation(app):
    a, c = app
    session = login(c)
    assert c.get("/v1/session", headers=auth(session)).json()["extension"] == "1001"
    cred = c.post("/v1/devices/current/sip-credentials", headers=auth(session)).json()
    assert cred["authorization_user"] == "d" + session["device_id"]
    assert cred["web_socket_url"].startswith("wss://")
    bad = c.post("/v1/sessions", json={"password": "DO-NOT-ECHO"})
    assert bad.status_code == 422 and "DO-NOT-ECHO" not in bad.text
    assert (
        c.post(
            "/v1/sessions",
            json=dict(username="alice", password="wrong", device_name="x", platform="android"),
        ).status_code
        == 401
    )
    assert c.post("/v1/sessions", content="a" * 16385).status_code == 413


def test_device_permissions_revoke_and_quota(app):
    a, c = app
    alice = login(c)
    bob = login(c, "bob")
    second = login(c, "bob", "second")
    assert c.delete("/v1/devices/" + bob["device_id"], headers=auth(alice)).status_code == 404
    assert (
        c.post(
            "/v1/admin/users",
            headers=auth(bob),
            json=dict(username="eve", extension="1003", password="long-password"),
        ).status_code
        == 403
    )
    assert (
        c.patch(
            "/v1/admin/users/" + bob["user_id"], headers=auth(alice), json={"device_limit": 2}
        ).status_code
        == 200
    )
    result = c.post(
        "/v1/sessions",
        json=dict(
            username="bob", password="test-only-password", device_name="third", platform="android"
        ),
    )
    assert result.status_code == 409
    assert c.delete("/v1/devices/" + second["device_id"], headers=auth(bob)).status_code == 200
    assert c.get("/v1/session", headers=auth(second)).status_code == 401
    assert (
        c.patch(
            "/v1/admin/users/" + bob["user_id"], headers=auth(alice), json={"status": "disabled"}
        ).status_code
        == 200
    )
    assert c.get("/v1/session", headers=auth(bob)).status_code == 401


def test_outbound_idempotency_and_cancel(app):
    a, c = app
    session = login(c)

    def post():
        return c.post("/v1/outbound-tasks", headers=auth(session), json={"destination": "123456"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: post(), range(4)))
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()["id"] for r in results}) == 1
    assert (
        c.post(
            "/v1/outbound-tasks", headers=auth(session), json={"destination": "654321"}
        ).status_code
        == 409
    )
    task = results[0].json()
    assert task["position"] == 1 and task["estimated_wait_seconds"] is None
    for _ in range(2):
        assert (
            c.post(
                "/v1/outbound-tasks/" + task["id"] + "/cancel", headers=auth(session)
            ).status_code
            == 200
        )
    with a.state.service.database.transaction(False) as db:
        assert db.scalar(select(func.count()).select_from(m.OutboundTask)) == 1


def test_simultaneous_claim_is_not_connection(app):
    a, c = app
    one = login(c)
    two = login(c, name="second")
    call = seed_call(a, [one, two])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda s: c.post(f"/v1/calls/{call}/claim", headers=auth(s)), [one, two])
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(r.json() for r in results if r.status_code == 200)
    assert winner["state"] == "claimed"
    assert (
        c.post(
            f"/v1/calls/{call}/device-events",
            headers=auth(one, "connected"),
            json={"stage": "connected"},
        ).status_code
        == 200
    )
    assert c.get(f"/v1/calls/{call}", headers=auth(one)).json()["state"] == "claimed"
    assert (
        c.post(
            "/v1/calls/resolve", headers=auth(one), json={"sip_call_id": "wire-" + one["device_id"]}
        ).json()["id"]
        == call
    )
    assert c.get(f"/v1/calls/{call}", headers=auth(login(c, "bob"))).status_code == 404


def test_internal_caller_winner_is_leg_specific(app):
    a, c = app
    caller = login(c)
    callee = login(c, "bob")
    call = seed_call(a, [callee], "internal")
    with a.state.service.database.transaction() as db:
        row = db.get(m.Call, call)
        row.origin_device_id = caller["device_id"]
        row.winner_device_id = callee["device_id"]
        db.add(
            m.Leg(
                call_id=call,
                device_id=caller["device_id"],
                role="origin",
                state="up",
                sip_call_id="origin-wire",
            )
        )
    payload = c.get(f"/v1/calls/{call}", headers=auth(caller)).json()
    assert payload["answered_device_id"] == caller["device_id"] and payload["remote"] == "1002"


def test_push_binding_and_websocket(app):
    a, c = app
    one = login(c)
    two = login(c, name="second")
    assert (
        c.put(
            "/v1/devices/current/push-token",
            headers=auth(one),
            json={"provider": "fcm", "token": "private-fcm"},
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/v1/devices/current/push-token",
            headers=auth(two),
            json={"provider": "fcm", "token": "private-fcm"},
        ).status_code
        == 409
    )
    with a.state.service.database.transaction(False) as db:
        d = db.get(m.Device, one["device_id"])
        assert "private-fcm" not in d.push_token
    with c.websocket_connect("/v1/events", headers=auth(one)) as ws:
        assert ws.receive_json()["type"] == "snapshot_changed"
    assert c.get("/v1/snapshot", headers=auth(one)).json()["history"] == []


def test_device_inventory_is_not_an_implicit_product_limit(app):
    a, c = app
    session = login(c)
    with a.state.service.database.transaction() as db:
        for i in range(205):
            db.add(
                m.Device(
                    user_id=session["user_id"],
                    name=f"old device {i}",
                    platform="android",
                    sip_secret=a.state.service.vault.encrypt("test-only-secret"),
                )
            )
    own = c.get("/v1/devices", headers=auth(session))
    assert own.status_code == 200 and len(own.json()) == 206
    assert len(c.get("/v1/admin/devices", headers=auth(session)).json()) == 206
    assert c.get("/v1/session", headers=auth(session)).json()["device_limit"] is None


def test_reauthentication_releases_expired_device_quota_without_pbx(app):
    a, c = app
    session = login(c)
    with a.state.service.database.transaction() as db:
        db.get(m.User, session["user_id"]).device_limit = 1
        db.scalar(
            select(m.AuthSession).where(m.AuthSession.device_id == session["device_id"])
        ).expires_at = time.time() - 1
    a.state.runtime.ready = False
    renewed = login(c)
    assert renewed["device_id"] != session["device_id"]
    with a.state.service.database.transaction(False) as db:
        assert db.get(m.Device, session["device_id"]).status == "revoked"
    assert c.get("/v1/session", headers=auth(session)).status_code == 401
