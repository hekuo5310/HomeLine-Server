# SPDX-License-Identifier: MPL-2.0
import base64
import json
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect


class PBXError(Exception):
    pass


class Asterisk:
    def __init__(self, settings):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.ari_url + "/",
            auth=(settings.ari_user, settings.ari_password.get_secret_value()),
            timeout=5,
            trust_env=False,
            follow_redirects=False,
        )

    async def request(self, method, path, params=None, body=None, missing=False, conflict=False):
        try:
            r = await self.http.request(method, path, params=params, json=body)
            if missing and r.status_code == 404:
                return None
            if conflict and r.status_code == 409:
                return None
            r.raise_for_status()
            return r.json() if r.content else None
        except (httpx.HTTPError, ValueError) as exc:
            raise PBXError("asterisk_request_failed") from exc

    async def info(self):
        return await self.request("GET", "asterisk/info")

    async def channels(self):
        return await self.request("GET", "channels")

    async def variable(self, channel, name):
        r = await self.request(
            "GET", f"channels/{channel}/variable", {"variable": name}, missing=True
        )
        return r.get("value", "") if r else ""

    async def wire_id(self, channel):
        return channel.get("protocol_id") or await self.variable(
            channel["id"], "CHANNEL(pjsip,call-id)"
        )

    async def create(self, channel, endpoint, args, caller):
        r = await self.request(
            "POST",
            "channels/create",
            {"channelId": channel, "endpoint": endpoint, "app": "homeline", "appArgs": args},
            {"variables": {"CALLERID(num)": caller}},
            conflict=True,
        )
        return r if r else await self.request("GET", "channels/" + channel)

    async def dial(self, channel, timeout):
        return await self.request("POST", f"channels/{channel}/dial", {"timeout": timeout})

    async def answer(self, channel):
        return await self.request("POST", f"channels/{channel}/answer")

    async def ring(self, channel):
        return await self.request("POST", f"channels/{channel}/ring")

    async def hangup(self, channel, reason="normal"):
        return await self.request(
            "DELETE", f"channels/{channel}", {"reason": reason}, missing=True, conflict=True
        )

    async def bridge(self, bridge, channels):
        await self.request("POST", "bridges/" + bridge, {"type": "mixing"}, conflict=True)
        await self.request("POST", f"bridges/{bridge}/addChannel", {"channel": ",".join(channels)})

    async def destroy_bridge(self, bridge):
        await self.request("DELETE", "bridges/" + bridge, missing=True, conflict=True)

    async def reload(self):
        await self.request("PUT", "asterisk/modules/res_pjsip.so")

    async def events(self):
        parts = urlsplit(self.settings.ari_url)
        url = urlunsplit(
            (
                "wss" if parts.scheme == "https" else "ws",
                parts.netloc,
                parts.path + "/events",
                "app=homeline&subscribeAll=true",
                "",
            )
        )
        # Basic authorization headers, never credentials in event URLs/logs.
        encoded = base64.b64encode(
            (self.settings.ari_user + ":" + self.settings.ari_password.get_secret_value()).encode()
        ).decode()
        headers = {"Authorization": "Basic " + encoded}
        async with connect(
            url, additional_headers=headers, proxy=None, max_size=1048576, ping_interval=20
        ) as ws:
            yield {"type": "HomeLineConnected"}
            async for message in ws:
                yield json.loads(message)

    async def close(self):
        await self.http.aclose()
