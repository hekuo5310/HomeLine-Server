# SPDX-License-Identifier: MPL-2.0
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HOMELINE_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    database: Path = Path("/var/lib/homeline/homeline.sqlite3")
    key_file: Path = Path("/etc/homeline/master.key")
    pjsip_file: Path = Path("/etc/asterisk/homeline/devices.conf")
    ari_url: str = "http://127.0.0.1:8088/ari"
    ari_user: str = "homeline"
    ari_password: SecretStr
    sip_wss_url: str
    sip_domain: str
    sip_realm: str = "homeline"
    ice_servers: list[dict[str, str]] = []
    session_seconds: int = 86400
    credential_seconds: int = 600
    ring_seconds: int = 45
    claim_seconds: int = 15
    incoming_extension: str = "1001"
    fxo_endpoint: str = "homeline-fxo"
    fxo_enabled: bool = False
    firebase_credentials: Path | None = None
    worker_enabled: bool = True
    lock_file: Path = Path("/var/lib/homeline/worker.lock")
    trusted_hosts: list[str] = ["localhost", "127.0.0.1"]

    @field_validator("ari_url")
    @classmethod
    def local_ari(cls, value):
        u = urlsplit(value)
        if (
            u.scheme not in {"http", "https"}
            or u.hostname not in {"localhost", "127.0.0.1", "::1"}
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("ARI must be a credential-free loopback URL")
        return value.rstrip("/")

    @field_validator("sip_wss_url")
    @classmethod
    def secure_sip(cls, value):
        u = urlsplit(value)
        if u.scheme != "wss" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("SIP requires WSS")
        return value

    @field_validator("sip_domain", "sip_realm", "fxo_endpoint")
    @classmethod
    def identifier(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
            raise ValueError("invalid identifier")
        return value

    @field_validator("session_seconds", "credential_seconds", "ring_seconds", "claim_seconds")
    @classmethod
    def duration(cls, value):
        if value <= 0:
            raise ValueError("duration must be positive")
        return value
