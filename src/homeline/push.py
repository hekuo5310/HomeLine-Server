# SPDX-License-Identifier: MPL-2.0
import asyncio
from datetime import timedelta

import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin.exceptions import FirebaseError


class PushFailure(Exception):
    def __init__(self, code, permanent=False, invalid_token=False):
        super().__init__(code)
        self.permanent, self.invalid_token = permanent, invalid_token


class FirebasePush:
    def __init__(self, settings):
        self.app = None
        if settings.firebase_credentials:
            self.app = firebase_admin.initialize_app(
                credentials.Certificate(str(settings.firebase_credentials)),
                options={"httpTimeout": 5},
                name="homeline",
            )

    async def send(self, token, data, ttl):
        if not self.app:
            raise PushFailure("fcm_not_configured", permanent=True)
        message = messaging.Message(
            token=token,
            data=data,
            android=messaging.AndroidConfig(
                priority="high", ttl=timedelta(seconds=max(1, min(120, ttl)))
            ),
        )
        try:
            return await asyncio.to_thread(messaging.send, message, app=self.app)
        except messaging.UnregisteredError as exc:
            raise PushFailure("invalid_token", permanent=True, invalid_token=True) from exc
        except (messaging.SenderIdMismatchError, ValueError) as exc:
            raise PushFailure("fcm_configuration_error", permanent=True) from exc
        except FirebaseError as exc:
            raise PushFailure("fcm_send_failed") from exc

    async def close(self):
        if self.app:
            firebase_admin.delete_app(self.app)
