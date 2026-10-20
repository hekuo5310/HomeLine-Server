# SPDX-License-Identifier: MPL-2.0
import time
from datetime import datetime

import pytest
from sqlalchemy import select
from test_api import auth, login

from homeline import models as m


@pytest.mark.parametrize("line_state", ["unknown", "idle", "busy"])
def test_status_tracks_pbx_and_line_without_claiming_device_registration(app, line_state):
    a, c = app
    session = login(c)
    with a.state.service.database.transaction() as db:
        db.add(m.LineLease(line_id="fxo", state=line_state))
    result = c.get("/v1/status", headers=auth(session))
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    payload = result.json()
    assert payload["server_status"] == "ready" and payload["pbx_ready"] is True
    assert payload["line_status"] == line_state
    assert payload["extension"] == "1001"
    assert payload["active_calls"] == payload["waiting_outbound_tasks"] == 0
    assert datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00")).tzinfo
    assert "password" not in result.text and session["access_token"] not in result.text
    snapshot = c.get("/v1/snapshot", headers=auth(session)).json()
    for field in ("account_status", "device_status", "line_status", "can_call"):
        assert payload[field] == snapshot[field]
    a.state.runtime.ready = False
    offline = c.get("/v1/status", headers=auth(session))
    assert offline.status_code == 200
    assert offline.json()["server_status"] == "degraded"
    assert offline.json()["pbx_ready"] is False
    assert offline.json()["line_status"] == "offline"
    assert offline.json()["can_call"] is False
    assert c.get("/health/ready").status_code == 503
    a.state.runtime.ready = True
    a.state.service.settings.fxo_enabled = False
    assert c.get("/v1/status", headers=auth(session)).json()["line_status"] == "offline"
    assert c.get("/v1/status", headers=auth(session)).json()["fxo_enabled"] is False


def test_status_aggregates_all_rows_with_device_and_user_scopes(app):
    a, c = app
    alice = login(c)
    other_device = login(c, name="second")
    bob = login(c, "bob")
    with a.state.service.database.transaction() as db:
        for session, states in [
            (alice, ["waiting"] * 205 + ["ringing", "connected", "ended", "failed"]),
            (other_device, ["waiting"]),
            (bob, ["waiting"]),
        ]:
            for state in states:
                call = m.Call(
                    user_id=session["user_id"],
                    kind="external",
                    state=state,
                    remote="private-number",
                    deadline=time.time() + 45,
                )
                db.add(call)
                db.flush()
                db.add(m.Leg(call_id=call.id, device_id=session["device_id"]))
            for state in ("waiting", "dialing", "completed", "cancelled"):
                db.add(
                    m.OutboundTask(
                        user_id=session["user_id"],
                        device_id=session["device_id"],
                        destination="123456",
                        state=state,
                    )
                )
    response = c.get("/v1/status", headers=auth(alice))
    payload = response.json()
    assert payload["active_calls"] == 207
    assert payload["waiting_inbound_calls"] == 205
    assert payload["waiting_outbound_tasks"] == 2  # Both devices of this user only.
    assert "private-number" not in response.text
    assert c.get("/v1/status", headers=auth(bob)).json()["active_calls"] == 1


@pytest.mark.parametrize("invalid", ["missing", "expired", "revoked", "disabled"])
def test_status_requires_current_authority(app, invalid):
    a, c = app
    session = login(c)
    with a.state.service.database.transaction() as db:
        if invalid == "expired":
            db.scalar(
                select(m.AuthSession).where(m.AuthSession.device_id == session["device_id"])
            ).expires_at = 0
        elif invalid == "revoked":
            db.get(m.Device, session["device_id"]).status = "revoked"
        elif invalid == "disabled":
            db.get(m.User, session["user_id"]).status = "disabled"
    result = c.get("/v1/status", headers={} if invalid == "missing" else auth(session))
    assert result.status_code == (401 if invalid in {"missing", "expired"} else 403)
    assert "extension" not in result.json()
