# SPDX-License-Identifier: MPL-2.0
"""Opt-in real Asterisk/WSS/DTLS-SRTP test; never a fake PBX substitute.
HOMELINE_TEST_ASTERISK_PREFIX=/path/to/independent/prefix pytest tests/native -q -s
Requires aiortc==1.14.0, no physical Android/FXO/FCM claim is made.
"""

import asyncio
import hashlib
import os
import re
import secrets
import ssl
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HOMELINE_TEST_ASTERISK_PREFIX"),
    reason="Independent Asterisk prefix not supplied",
)


def parse(message):
    head, _, body = message.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers.setdefault(key.lower(), []).append(value.strip())
    return lines[0], headers, body


def header(headers, name):
    return headers.get(name.lower(), [""])[0]


def digest_auth(challenge, user, password, method, uri):
    values = dict(re.findall(r'(\w+)="([^"]*)"', challenge))

    def md5(s):
        return hashlib.md5(s.encode()).hexdigest()  # SIP Digest interoperability

    realm, nonce = values["realm"], values["nonce"]
    cnonce = secrets.token_hex(8)
    a1 = md5(f"{user}:{realm}:{password}")
    a2 = md5(f"{method}:{uri}")
    if "qop" in values:
        response = md5(f"{a1}:{nonce}:00000001:{cnonce}:auth:{a2}")
        extra = f', qop=auth, nc=00000001, cnonce="{cnonce}"'
    else:
        response = md5(f"{a1}:{nonce}:{a2}")
        extra = ""
    return (
        f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}"'
        + extra
    )


class Phone:
    def __init__(self, credentials, ssl_context, port, relay_only=False):
        from aiortc import AudioStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection

        self.credentials = credentials
        self.user = credentials["authorization_user"]
        self.ssl_context, self.port = ssl_context, port
        self.relay_only = relay_only
        self.pc = RTCPeerConnection(
            RTCConfiguration(
                iceServers=[RTCIceServer(**s) for s in credentials.get("ice_servers", [])]
            )
        )
        self.pc.addTrack(AudioStreamTrack())
        self.responses = {}
        self.invite = asyncio.Queue()
        self.cancelled = asyncio.Event()
        self.listener = None

    def sdp(self, value):
        if not self.relay_only:
            return value
        candidates = [line for line in value.splitlines() if line.startswith("a=candidate:")]
        assert any(" typ relay" in line for line in candidates), (
            "Actual TURN relay candidate missing"
        )
        # Neither side can fall back to a direct host/srflx route in this test.
        return (
            "\r\n".join(
                line
                for line in value.splitlines()
                if not line.startswith("a=candidate:") or " typ relay" in line
            )
            + "\r\n"
        )

    async def start(self):
        from websockets.asyncio.client import connect

        self.ws = await connect(
            f"wss://localhost:{self.port}/ws",
            ssl=self.ssl_context,
            subprotocols=["sip"],
            proxy=None,
        )
        self.listener = asyncio.create_task(self.listen())
        uri = "sip:localhost"
        call_id = secrets.token_hex(16)
        first = await self.transact("REGISTER", uri, call_id, 1)
        assert first[0].startswith("SIP/2.0 401"), first[0]
        authorization = digest_auth(
            header(first[1], "www-authenticate"),
            self.user,
            self.credentials["password"],
            "REGISTER",
            uri,
        )
        result = await self.transact(
            "REGISTER", uri, call_id, 2, extra="Authorization: " + authorization + "\r\n"
        )
        assert result[0].startswith("SIP/2.0 200"), result[0]

    def request(self, method, uri, call_id, sequence, extra="", body="", to=None, from_value=None):
        return f"{method} {uri} SIP/2.0\r\nVia: SIP/2.0/WSS localhost;branch=z9hG4bK{secrets.token_hex(8)}\r\nMax-Forwards: 70\r\nFrom: {from_value or f'<sip:{self.user}@localhost>;tag=client'}\r\nTo: {to or f'<sip:{self.user}@localhost>'}\r\nCall-ID: {call_id}\r\nCSeq: {sequence} {method}\r\nContact: <sip:{self.user}@localhost;transport=ws>\r\nExpires: 60\r\n{extra}Content-Length: {len(body.encode())}\r\n\r\n{body}"

    async def transact(self, method, uri, call_id, sequence, extra="", body="", to=None):
        queue = asyncio.Queue()
        self.responses[(call_id, f"{sequence} {method}")] = queue
        await self.ws.send(self.request(method, uri, call_id, sequence, extra, body, to))
        while True:
            result = await asyncio.wait_for(queue.get(), 15)
            if int(result[0].split()[1]) >= 200:
                return result

    async def respond(self, request, code, reason, body=""):
        _, h, _ = request
        to = header(h, "to")
        if ";tag=" not in to:
            to += ";tag=" + self.user[-8:]
        headers = "".join(
            f"{name}: {value}\r\n"
            for name in ["via", "from", "call-id", "cseq"]
            for value in h.get(name, [])
        )
        content_type = "Content-Type: application/sdp\r\n" if body else ""
        await self.ws.send(
            f"SIP/2.0 {code} {reason}\r\n{headers}To: {to}\r\nContact: <sip:{self.user}@localhost;transport=ws>\r\n{content_type}Content-Length: {len(body.encode())}\r\n\r\n{body}"
        )

    async def listen(self):
        async for message in self.ws:
            result = parse(message)
            first, h, _ = result
            if first.startswith("SIP/2.0"):
                queue = self.responses.get((header(h, "call-id"), header(h, "cseq")))
                if queue:
                    await queue.put(result)
            elif first.startswith("INVITE "):
                self.incoming = result
                await self.respond(result, 180, "Ringing")
                await self.invite.put(result)
            elif first.startswith("CANCEL "):
                await self.respond(result, 200, "OK")
                await self.respond(self.incoming, 487, "Request Terminated")
                self.cancelled.set()
            elif first.startswith(("OPTIONS ", "BYE ")):
                await self.respond(result, 200, "OK")

    async def answer(self, incoming):
        from aiortc import RTCSessionDescription

        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=self.sdp(incoming[2]), type="offer")
        )
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        await self.respond(incoming, 200, "OK", self.sdp(self.pc.localDescription.sdp))

    async def originate(self, extension):
        from aiortc import RTCSessionDescription

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        uri = f"sip:{extension}@localhost"
        call_id = secrets.token_hex(16)
        to = f"<{uri}>"
        extra = "Content-Type: application/sdp\r\n"
        result = await self.transact(
            "INVITE", uri, call_id, 1, extra, self.sdp(self.pc.localDescription.sdp), to
        )
        sequence = 1
        if result[0].startswith(("SIP/2.0 401", "SIP/2.0 407")):
            await self.ws.send(self.request("ACK", uri, call_id, 1, to=header(result[1], "to")))
            proxy = result[0].startswith("SIP/2.0 407")
            challenge = header(result[1], "proxy-authenticate" if proxy else "www-authenticate")
            extra += (
                ("Proxy-Authorization" if proxy else "Authorization")
                + ": "
                + digest_auth(challenge, self.user, self.credentials["password"], "INVITE", uri)
                + "\r\n"
            )
            sequence = 2
            result = await self.transact(
                "INVITE", uri, call_id, sequence, extra, self.sdp(self.pc.localDescription.sdp), to
            )
        assert result[0].startswith("SIP/2.0 200"), result[0]
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=self.sdp(result[2]), type="answer")
        )
        contact = re.search(r"<([^>]+)>", header(result[1], "contact")).group(1)
        await self.ws.send(
            self.request("ACK", contact, call_id, sequence, to=header(result[1], "to"))
        )
        return call_id

    async def close(self):
        await self.pc.close()
        if self.listener:
            self.listener.cancel()
        if hasattr(self, "ws"):
            await self.ws.close()
        if self.listener:
            await asyncio.gather(self.listener, return_exceptions=True)


def native_config(root, prefix):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    (root / "tls.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (root / "tls.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for directory in ("run", "logs", "data", "spool"):
        (root / directory).mkdir()
    (root / "asterisk.conf").write_text(f"""[directories]
astetcdir => {root}
astmoddir => {prefix}/lib/asterisk/modules
astvarlibdir => {root}/data
astdbdir => {root}/data
astkeydir => {root}/data
astdatadir => {prefix}/var/lib/asterisk
astspooldir => {root}/spool
astrundir => {root}/run
astlogdir => {root}/logs
[options]
systemname = homeline-native-test
""")
    (root / "http.conf").write_text(
        f"[general]\nenabled=yes\nbindaddr=127.0.0.1\nbindport=18088\ntlsenable=yes\ntlsbindaddr=127.0.0.1:18089\ntlscertfile={root}/tls.pem\ntlsprivatekey={root}/tls.key\n"
    )
    password = secrets.token_urlsafe(32)
    (root / "ari.conf").write_text(
        f"[general]\nenabled=yes\n[homeline]\ntype=user\nread_only=no\npassword={password}\n"
    )
    (root / "manager.conf").write_text("[general]\nenabled=no\n")
    (root / "modules.conf").write_text(
        "[modules]\nautoload=yes\nnoload=chan_sip.so\nnoload=pbx_spool.so\n"
    )
    (root / "logger.conf").write_text("[logfiles]\nconsole=warning,error,notice\n")
    (root / "rtp.conf").write_text("[general]\nrtpstart=21000\nrtpend=21100\nicesupport=yes\n")
    (root / "pjsip.conf").write_text(
        "[global]\ntype=global\nendpoint_identifier_order=auth_username,username,ip\n[homeline-wss]\ntype=transport\nprotocol=wss\nbind=127.0.0.1\n#include devices.conf\n"
    )
    (root / "devices.conf").write_text("; initial\n")
    (root / "extensions.conf").write_text(
        "[homeline-devices]\nexten => _X.,1,Stasis(homeline,internal,${HOMELINE_DEVICE_ID},${EXTEN})\n same => n,Hangup()\n"
    )
    return password


async def wait_until(predicate, seconds=15):
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.1)
    raise AssertionError("Native condition timed out")


@pytest.mark.parametrize(
    ("media_required", "proxy"),
    [(False, False), (True, False), (True, True)],
    ids=["control", "media", "proxy-media"],
)
def test_native_multidevice_media_and_restart(tmp_path, media_required, proxy):
    if proxy and not os.environ.get("HOMELINE_TEST_NGINX"):
        pytest.skip("Real Nginx executable not supplied")
    if media_required and os.environ.get("HOMELINE_TEST_CONTROL_ONLY") == "1":
        pytest.skip("Runner forbids PBX interface enumeration; media needs a network-enabled host")
    if os.environ.get("HOMELINE_TEST_LOOPBACK") == "1":
        from unittest.mock import patch

        # Isolated runner disallows interface enumeration. Use real loopback UDP,
        # without replacing any SIP, PBX, ICE, DTLS or media implementation.
        with patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]):
            asyncio.run(scenario(tmp_path, media_required, proxy))
    else:
        asyncio.run(scenario(tmp_path, media_required, proxy))


def test_native_turn_only_media(tmp_path):
    if not os.environ.get("HOMELINE_TEST_TURN") or not os.environ.get("HOMELINE_TEST_NGINX"):
        pytest.skip("Real coturn and Nginx executables not supplied")

    async def run():
        from turn_stack import turn_server

        async with turn_server(tmp_path) as turn:
            await scenario(tmp_path, True, True, turn)

    asyncio.run(run())


async def scenario(root, media_required, proxy, turn=None):
    from cryptography.fernet import Fernet
    from proxy_stack import api_client

    from homeline import models as m
    from homeline.api import create_app
    from homeline.cli import migrate
    from homeline.config import Settings
    from homeline.security import hasher

    prefix = Path(os.environ["HOMELINE_TEST_ASTERISK_PREFIX"])
    password = native_config(root, prefix)
    if turn:
        from homeline.networking import rtp_configuration

        (root / "rtp.conf").write_text(rtp_configuration(turn, 21000, 21100))
    output = (root / "native.log").open("w")

    def launch():
        return subprocess.Popen(
            [str(prefix / "sbin/asterisk"), "-f", "-C", str(root / "asterisk.conf")],
            stdout=output,
            stderr=subprocess.STDOUT,
        )

    process = launch()
    phones = []
    (root / "master.key").write_bytes(Fernet.generate_key())
    (root / "master.key").chmod(0o600)
    settings = Settings(
        database=root / "state.sqlite3",
        key_file=root / "master.key",
        ari_url="http://127.0.0.1:18088/ari",
        ari_password=password,
        sip_wss_url="wss://localhost:18443/ws" if proxy else "wss://localhost:18089/ws",
        sip_domain="localhost",
        pjsip_file=root / "devices.conf",
        lock_file=root / "worker.lock",
        trusted_hosts=["testserver", "localhost"],
        ring_seconds=60,
        ice_servers=turn["ice_servers"] if turn else [],
    )
    migrate(settings)
    app = create_app(settings)
    rt = app.state.runtime
    with rt.database.transaction() as db:
        for name, extension in [("alice", "1001"), ("bob", "1002")]:
            db.add(
                m.User(
                    username=name,
                    extension=extension,
                    password_hash=hasher.hash("test-only-password"),
                )
            )
    try:
        async with (
            app.router.lifespan_context(app),
            api_client(app, root, proxy) as (client, sip_port),
        ):
            await wait_until(lambda: rt.ready, 30)
            from websockets.asyncio.client import connect

            with pytest.raises(ssl.SSLCertVerificationError):
                async with connect(
                    settings.sip_wss_url,
                    ssl=ssl.create_default_context(),
                    subprotocols=["sip"],
                    proxy=None,
                ):
                    raise AssertionError("Untrusted certificate accepted")
            from unittest.mock import patch

            from homeline.diagnostics import diagnose

            with patch.dict(os.environ, {"SSL_CERT_FILE": str(root / "tls.pem")}):
                diagnostic = await diagnose(settings)
                assert diagnostic["trusted_sip_wss"] == "ok"
            context = ssl.create_default_context(cafile=str(root / "tls.pem"))
            if proxy:
                from websockets.exceptions import InvalidStatus

                with pytest.raises(InvalidStatus) as rejected:
                    async with connect(
                        "wss://localhost:18444/ws", ssl=context, subprotocols=["sip"], proxy=None
                    ):
                        raise AssertionError("Nginx accepted mismatched upstream certificate")
                assert rejected.value.response.status_code == 502
            sessions = []
            for username in ("alice", "bob", "bob"):
                response = await client.post(
                    "/v1/sessions",
                    json=dict(
                        username=username,
                        password="test-only-password",
                        device_name="native test",
                        platform="linux",
                    ),
                )
                assert response.status_code == 201, response.text
                session = response.json()
                sessions.append(session)
                credentials = await client.post(
                    "/v1/devices/current/sip-credentials",
                    headers={"Authorization": "Bearer " + session["access_token"]},
                )
                assert credentials.status_code == 200, credentials.text
                phone = Phone(credentials.json(), context, sip_port, relay_only=bool(turn))
                phones.append(phone)
                await phone.start()
            status_headers = {"Authorization": "Bearer " + sessions[0]["access_token"]}
            assert (await client.get("/v1/status")).status_code == 401
            status = await client.get("/v1/status", headers=status_headers)
            assert status.status_code == 200
            assert status.json()["server_status"] == "ready"
            assert status.json()["active_calls"] == 0
            if proxy:
                async with connect(
                    "wss://localhost:18443/v1/events",
                    ssl=context,
                    additional_headers={"Authorization": "Bearer " + sessions[0]["access_token"]},
                    proxy=None,
                ) as events:
                    assert (
                        __import__("json").loads(await asyncio.wait_for(events.recv(), 5))["type"]
                        == "snapshot_changed"
                    )
            origin = asyncio.create_task(phones[0].originate("1002"))

            def report_origin(task):
                if not task.cancelled() and task.exception():
                    print("ORIGIN FAILURE:", repr(task.exception()), flush=True)

            origin.add_done_callback(report_origin)
            incoming = await asyncio.gather(
                *[asyncio.wait_for(p.invite.get(), 15) for p in phones[1:]]
            )
            call_ids = []
            for session, invite in zip(sessions[1:], incoming):
                r = await client.post(
                    "/v1/calls/resolve",
                    headers={"Authorization": "Bearer " + session["access_token"]},
                    json={"sip_call_id": header(invite[1], "call-id")},
                )
                assert r.status_code == 200, r.text
                assert r.json()["sip_call_id"] == header(invite[1], "call-id")
                call_ids.append(r.json()["id"])
            assert len(set(call_ids)) == 1
            call_id = call_ids[0]
            claims = await asyncio.gather(
                *[
                    client.post(
                        f"/v1/calls/{call_id}/claim",
                        headers={
                            "Authorization": "Bearer " + s["access_token"],
                            "Idempotency-Key": "native-claim",
                        },
                    )
                    for s in sessions[1:]
                ]
            )
            assert sorted(r.status_code for r in claims) == [200, 409]
            winner = 0 if claims[0].status_code == 200 else 1
            assert claims[winner].json()["state"] == "claimed"
            await phones[winner + 1].answer(incoming[winner])
            await asyncio.wait_for(origin, 15)
            await asyncio.wait_for(phones[(1 - winner) + 1].cancelled.wait(), 15)
            with rt.database.transaction(False) as db:
                assert db.get(m.Call, call_id).state == "connected"
            if media_required:
                await wait_until(
                    lambda: (
                        phones[0].pc.connectionState == "connected"
                        and phones[winner + 1].pc.connectionState == "connected"
                    )
                )

                async def media_received():
                    stats = await phones[winner + 1].pc.getStats()
                    return any(
                        s.type == "inbound-rtp" and s.packetsReceived > 10 for s in stats.values()
                    )

                await wait_until(media_received)
            loser_index = (1 - winner) + 1
            revoked = await client.delete(
                "/v1/devices/" + sessions[loser_index]["device_id"],
                headers={
                    "Authorization": "Bearer " + sessions[winner + 1]["access_token"],
                    "Idempotency-Key": "native-revoke",
                },
            )
            assert revoked.status_code == 200, revoked.text
            old_session = await client.get(
                "/v1/session",
                headers={"Authorization": "Bearer " + sessions[loser_index]["access_token"]},
            )
            assert old_session.status_code == 401
            old_credentials = Phone(phones[loser_index].credentials, context, sip_port)
            phones.append(old_credentials)
            with pytest.raises(AssertionError, match=r"SIP/2.0 (401|403|404)"):
                await old_credentials.start()
            process.kill()
            await asyncio.to_thread(process.wait, 5)
            await wait_until(lambda: not rt.ready)
            status = await client.get("/v1/status", headers=status_headers)
            assert status.status_code == 200
            assert status.json()["server_status"] == "degraded"
            assert status.json()["line_status"] == "offline"
            process = launch()
            await wait_until(lambda: rt.ready, 30)
            status = await client.get("/v1/status", headers=status_headers)
            assert status.json()["server_status"] == "ready"
            assert status.json()["active_calls"] == 0
            with rt.database.transaction(False) as db:
                assert db.get(m.Call, call_id).state in {"ended", "failed"}
            assert not await rt.pbx.channels()
            print(
                "PASS: WSS REGISTER/digest, per-leg SIP Call-ID, simultaneous claim, loser CANCEL, revoked SIP credentials rejected, PBX restart cleanup; media verified="
                + str(media_required)
                + "; nginx proxy="
                + str(proxy)
                + "; relay-only="
                + str(bool(turn))
            )
    finally:
        for phone in phones:
            await phone.close()
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        output.close()
