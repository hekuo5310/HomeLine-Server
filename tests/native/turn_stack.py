# SPDX-License-Identifier: MPL-2.0
"""Isolated real coturn process; loopback peers are permitted ONLY in this test."""

import asyncio
import os
import secrets
import subprocess
from contextlib import asynccontextmanager


@asynccontextmanager
async def turn_server(root):
    user, password = "native-turn", secrets.token_urlsafe(24)
    config = root / "coturn.conf"
    config.write_text(f"""listening-ip=127.0.0.1
relay-ip=127.0.0.1
listening-port=13478
min-port=22000
max-port=22100
realm=homeline-native
fingerprint
lt-cred-mech
user={user}:{password}
no-tls
no-dtls
no-cli
no-multicast-peers
allow-loopback-peers
no-software-attribute
pidfile={root}/coturn.pid
log-file=stdout
""")
    config.chmod(0o600)
    output = (root / "coturn.log").open("w")
    process = subprocess.Popen(
        [os.environ["HOMELINE_TEST_TURN"], "-c", str(config)],
        stdout=output,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("Real coturn exited; inspect coturn.log")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", 13478)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("Real coturn did not start")
        yield {
            "server": "127.0.0.1:13478",
            "username": user,
            "password": password,
            "ice_servers": [
                {
                    "urls": "turn:127.0.0.1:13478?transport=tcp",
                    "username": user,
                    "credential": password,
                }
            ],
        }
    finally:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        output.close()
