# SPDX-License-Identifier: MPL-2.0
import asyncio
from types import SimpleNamespace

import pytest
from firebase_admin import messaging

from homeline.push import FirebasePush, PushFailure


def test_firebase_sdk_payload_and_invalid_token(monkeypatch):
    push = FirebasePush(SimpleNamespace(firebase_credentials=None))
    with pytest.raises(PushFailure, match="fcm_not_configured"):
        asyncio.run(push.send("test", {}, 10))
    push.app = object()
    sent = []

    def send(message, app):
        sent.append(message)
        raise messaging.UnregisteredError("expired")

    monkeypatch.setattr(messaging, "send", send)
    with pytest.raises(PushFailure) as error:
        asyncio.run(
            push.send(
                "test-token",
                {"type": "incoming_call", "call_id": "test", "expires_at": "test"},
                500,
            )
        )
    assert error.value.permanent and error.value.invalid_token
    assert sent[0].notification is None and sent[0].android.priority == "high"
    assert sent[0].android.ttl.total_seconds() == 120
