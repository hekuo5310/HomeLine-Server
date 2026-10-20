#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Render a dedicated PBX instance. Existing configuration is never overwritten."""

import argparse
import ipaddress
import json
import re
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from homeline.networking import load_turn_config, rtp_configuration
from homeline.provisioning import atomic_write


def create(path, text, mode=0o640):
    if not path.exists():
        atomic_write(path, text.encode() if isinstance(text, str) else text, mode)


def configure(root, host, cert, key, gateway=None, media_address=None, turn_config=None):
    turn = load_turn_config(turn_config) if turn_config else None
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,200}", host):
        raise ValueError("Invalid DNS host")
    for value in (cert, key):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
            raise ValueError("Invalid certificate path")
    if gateway:
        gateway = str(ipaddress.IPv4Address(gateway))
    if media_address:
        media_address = str(ipaddress.IPv4Address(media_address))
    config = root / "etc/homeline"
    pbx = config / "asterisk"
    pbx.mkdir(parents=True, exist_ok=True)
    if (root / "var/lib/homeline/homeline.sqlite3").exists() and not (
        config / "master.key"
    ).is_file():
        raise RuntimeError("Existing database requires its original master key; restore a backup")
    create(config / "master.key", Fernet.generate_key(), 0o600)
    create(pbx / "tls/server.crt", Path(cert).read_bytes())
    create(pbx / "tls/server.key", Path(key).read_bytes())
    env = config / "server.env"
    if not env.exists():
        settings = {
            "HOMELINE_DATABASE": "/var/lib/homeline/homeline.sqlite3",
            "HOMELINE_KEY_FILE": "/etc/homeline/master.key",
            "HOMELINE_PJSIP_FILE": "/etc/homeline/asterisk/generated/devices.conf",
            "HOMELINE_ARI_URL": "http://127.0.0.1:8088/ari",
            "HOMELINE_ARI_USER": "homeline",
            "HOMELINE_ARI_PASSWORD": secrets.token_urlsafe(48),
            "HOMELINE_SIP_DOMAIN": host,
            "HOMELINE_SIP_WSS_URL": f"wss://{host}/ws",
            "HOMELINE_TRUSTED_HOSTS": json.dumps([host, "localhost", "127.0.0.1"]),
            "HOMELINE_FXO_ENABLED": str(bool(gateway)).lower(),
            "HOMELINE_LOCK_FILE": "/var/lib/homeline/worker.lock",
            "HOMELINE_ICE_SERVERS": json.dumps(turn["ice_servers"] if turn else []),
        }
        create(env, "\n".join(k + "=" + json.dumps(v) for k, v in settings.items()) + "\n", 0o600)
    # Reuse generated secret on repeated installation, never rotate behind a running instance.
    from homeline.config import Settings

    settings = Settings(_env_file=env)
    create(
        pbx / "asterisk.conf",
        """[directories]
astetcdir => /etc/homeline/asterisk
astmoddir => /opt/homeline-asterisk/lib/asterisk/modules
astvarlibdir => /var/lib/homeline-asterisk
astdbdir => /var/lib/homeline-asterisk
astkeydir => /var/lib/homeline-asterisk/keys
astdatadir => /opt/homeline-asterisk/var/lib/asterisk
astagidir => /opt/homeline-asterisk/var/lib/asterisk/agi-bin
astspooldir => /var/spool/homeline-asterisk
astrundir => /run/homeline-asterisk
astlogdir => /var/log/homeline-asterisk
[options]
runuser = asterisk
rungroup = asterisk
systemname = homeline
""",
    )
    create(
        pbx / "http.conf",
        "[general]\nenabled=yes\nbindaddr=127.0.0.1\nbindport=8088\ntlsenable=yes\ntlsbindaddr=127.0.0.1:8089\ntlscertfile=/etc/homeline/asterisk/tls/server.crt\ntlsprivatekey=/etc/homeline/asterisk/tls/server.key\n",
    )
    create(
        pbx / "ari.conf",
        f"[general]\nenabled=yes\npretty=no\n[homeline]\ntype=user\nread_only=no\npassword={settings.ari_password.get_secret_value()}\n",
    )
    create(pbx / "manager.conf", "[general]\nenabled=no\n")
    create(
        pbx / "modules.conf",
        """[modules]
autoload=yes
noload=chan_sip.so
noload=pbx_spool.so
noload=res_ari_mailboxes.so
noload=res_pjsip_outbound_registration.so
""",
    )
    create(pbx / "rtp.conf", rtp_configuration(turn))
    create(
        pbx / "logger.conf",
        "[general]\n[logfiles]\nconsole=warning,error,notice\nmessages=warning,error,notice\n",
    )
    transport = """[global]
type=global
user_agent=HomeLine PBX
endpoint_identifier_order=auth_username,username,ip
[homeline-wss]
type=transport
protocol=wss
bind=127.0.0.1
"""
    if media_address:
        transport += f"external_media_address={media_address}\n"
    transport += "#include generated/devices.conf\n#include fxo.conf\n"
    create(pbx / "pjsip.conf", transport)
    create(pbx / "generated/devices.conf", "; Provisioned by HomeLine\n")
    fxo = "; FXO disabled: configure an authenticated peer on a trusted LAN/VPN.\n"
    if gateway:
        fxo = f"""[homeline-fxo-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
[homeline-fxo]
type=endpoint
transport=homeline-fxo-udp
context=homeline-fxo-in
disallow=all
allow=ulaw,alaw
aors=homeline-fxo
identify_by=ip
direct_media=no
[homeline-fxo]
type=aor
contact=sip:{gateway}:5060
[homeline-fxo-identify]
type=identify
endpoint=homeline-fxo
match={gateway}/32
"""
    create(pbx / "fxo.conf", fxo)
    create(
        pbx / "extensions.conf",
        """[general]
static=yes
writeprotect=yes
[homeline-devices]
exten => _X.,1,Stasis(homeline,internal,${HOMELINE_DEVICE_ID},${EXTEN})
 same => n,Hangup()
[homeline-fxo-in]
exten => _.,1,Stasis(homeline,external)
 same => n,Hangup()
exten => s,1,Stasis(homeline,external)
 same => n,Hangup()
""",
    )
    create(
        root / "etc/nginx/sites-available/homeline",
        f"""server {{
    listen 443 ssl;
    server_name {host};
    ssl_certificate {cert};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 16k;
    access_log off;
    location /ari {{ return 404; }}
    location = /ws {{
        proxy_pass https://127.0.0.1:8089/ws;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_server_name on;
        proxy_ssl_name {host};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 75s;
    }}
    location / {{
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 75s;
    }}
}}
""",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("/"))
    p.add_argument("--host", required=True)
    p.add_argument("--cert", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--gateway")
    p.add_argument("--media-address")
    p.add_argument("--turn-config", type=Path)
    a = p.parse_args()
    configure(a.root, a.host, a.cert, a.key, a.gateway, a.media_address, a.turn_config)
