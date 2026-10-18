# SPDX-License-Identifier: MPL-2.0
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Name = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")]
Number = Annotated[str, Field(pattern=r"^\+?[0-9]{3,20}$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: Name
    password: str = Field(min_length=1, max_length=256)
    device_name: Name
    platform: Literal["android", "ios", "windows", "macos", "linux"]


class Rename(Input):
    name: Name


class PushToken(Input):
    provider: Literal["fcm"]
    token: str | None = Field(default=None, min_length=1, max_length=4096)


class Resolve(Input):
    sip_call_id: str = Field(min_length=1, max_length=255)


class Receipt(Input):
    stage: Literal[
        "received", "restored", "registered", "ringing", "connected", "failed", "media_lost"
    ]


class Outbound(Input):
    destination: Number


class CreateUser(Input):
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    password: str = Field(min_length=12, max_length=256)
    extension: str = Field(pattern=r"^[0-9]{2,8}$")
    role: Literal["user", "admin"] = "user"
    device_limit: int | None = Field(default=None, ge=0)


class UpdateUser(Input):
    status: Literal["active", "disabled"] | None = None
    device_limit: int | None = Field(default=None, ge=0)
    password: str | None = Field(default=None, min_length=12, max_length=256)


class Priority(Input):
    priority: int = Field(ge=-100, le=100)


class SessionResponse(BaseModel):
    access_token: str
    expires_at: datetime
    user_id: str
    extension: str
    device_id: str
    account_status: str
    device_limit: int | None


class DeviceResponse(BaseModel):
    id: str
    name: str
    status: str
    last_seen_at: datetime


class CallResponse(BaseModel):
    id: str
    sip_call_id: str
    state: str
    remote: str
    updated_at: datetime
    revision: int
    answered_device_id: str | None
    queue_position: int | None
    waiting_since: datetime | None


class TaskResponse(BaseModel):
    id: str
    destination: str
    state: str
    revision: int
    position: int | None
    estimated_wait_seconds: int | None
    failure_reason: str | None


class SnapshotResponse(BaseModel):
    account_status: str
    device_status: str
    line_status: str
    can_call: bool
    devices: list[DeviceResponse]
    calls: list[CallResponse]
    outbound_tasks: list[TaskResponse]
    history: list[CallResponse]


class CredentialsResponse(BaseModel):
    web_socket_url: str
    uri: str
    authorization_user: str
    password: str
    expires_at: datetime
    ice_servers: list[dict[str, str]]
