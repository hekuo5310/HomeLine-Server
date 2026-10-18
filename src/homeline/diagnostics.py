# SPDX-License-Identifier: MPL-2.0
import sqlite3
import ssl

from websockets.asyncio.client import connect

from .asterisk import Asterisk


async def diagnose(settings):
    if not settings.database.is_file():
        raise RuntimeError("database_missing")
    with sqlite3.connect(f"file:{settings.database}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("database_integrity")
    pbx = Asterisk(settings)
    try:
        await pbx.info()
    finally:
        await pbx.close()
    # End-to-end TLS/SIP WebSocket handshake, including the reverse proxy upstream.
    async with connect(
        settings.sip_wss_url,
        ssl=ssl.create_default_context(),
        subprotocols=["sip"],
        proxy=None,
        open_timeout=8,
        close_timeout=3,
    ) as socket:
        if socket.subprotocol != "sip":
            raise RuntimeError("sip_websocket_subprotocol")
    return {
        "database": "ok",
        "ari_required_modules": "ok",
        "trusted_sip_wss": "ok",
        "firebase": "configured_not_delivery_verified"
        if settings.firebase_credentials
        else "not_configured",
        "media": "requires_call_test",
    }
