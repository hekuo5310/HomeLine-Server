#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
import json
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from homeline.api import create_app
from homeline.config import Settings

with tempfile.TemporaryDirectory() as folder:
    key = Path(folder) / "key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    app = create_app(
        Settings(
            key_file=key,
            ari_password="schema-export-only",
            sip_domain="localhost",
            sip_wss_url="wss://localhost/ws",
            worker_enabled=False,
        )
    )
    target = Path(__file__).resolve().parents[1] / "docs/openapi.json"
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
    print(
        "Exported",
        len(app.openapi()["paths"]),
        "HTTP API paths; websocket protocol documented separately",
    )
