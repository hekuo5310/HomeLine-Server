# SPDX-License-Identifier: MPL-2.0
"""Deployment-only TURN configuration; no credentials in diagnostics."""

import json
import os
import re
import stat
from pathlib import Path


def load_turn_config(path):
    path = Path(path)
    # Verify the opened file, then read that same descriptor: a path replacement
    # between a stat() and read_text() must not bypass ownership checks.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("TURN configuration must be owned by the installer and private (0600)")
        if info.st_size > 32768:
            raise ValueError("TURN configuration is too large")
        try:
            data = json.loads(stream.read(32769))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid TURN JSON") from None
    if not isinstance(data, dict) or set(data) != {"server", "username", "password", "ice_servers"}:
        raise ValueError("TURN requires server, username, password and ice_servers")
    match = re.fullmatch(r"([A-Za-z0-9.-]{1,200}):([0-9]{1,5})", str(data["server"]))
    if not match or not 1 <= int(match[2]) <= 65535:
        raise ValueError("Asterisk TURN server must be DNS-or-IPv4:port with TCP support")
    for field in ("username", "password"):
        if not isinstance(data[field], str) or not re.fullmatch(
            r"[A-Za-z0-9_@.+:/=!~-]{1,256}", data[field]
        ):
            raise ValueError("TURN server credentials contain unsupported configuration characters")
    servers = data["ice_servers"]
    if not isinstance(servers, list) or not 1 <= len(servers) <= 8:
        raise ValueError("Provide 1-8 client TURN servers")
    for server in servers:
        if not isinstance(server, dict) or set(server) != {"urls", "username", "credential"}:
            raise ValueError("Client TURN requires urls, username and credential")
        if any(
            not isinstance(v, str) or not 1 <= len(v) <= 4096 or any(ord(c) < 32 for c in v)
            for v in server.values()
        ):
            raise ValueError("Invalid client TURN values")
        url = re.fullmatch(
            r"turns?:[A-Za-z0-9.-]+:([0-9]{1,5})(?:\?transport=(udp|tcp))?", server["urls"]
        )
        if not url or not 1 <= int(url[1]) <= 65535:
            raise ValueError("Invalid client TURN URL")
        if server["urls"].startswith("turns:") and url[2] == "udp":
            raise ValueError("Secure client TURN requires TCP; TURN over DTLS/UDP is unsupported")
    return data


def rtp_configuration(turn=None, start=10000, end=10100):
    text = f"[general]\nrtpstart={start}\nrtpend={end}\nicesupport=yes\nstrictrtp=yes\n"
    if turn:
        text += f"turnaddr={turn['server']}\nturnusername={turn['username']}\nturnpassword={turn['password']}\n"
    return text
