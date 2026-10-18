# SPDX-License-Identifier: MPL-2.0
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import models as m
from . import schemas as s
from .db import Database
from .security import Vault, digest, hasher
from .service import Problem, Service, audit, change, command, utc


def create_app(settings=None, runtime=None):
    if settings is None:
        from .config import Settings

        settings = Settings()
    database, vault = Database(settings.database), Vault(settings.key_file)
    service = Service(database, settings, vault)
    if runtime is None:
        from .runtime import Runtime

        runtime = Runtime(service)

    @asynccontextmanager
    async def lifespan(app):
        with database.engine.connect() as conn:
            if conn.execute(text("SELECT version_num FROM alembic_version")).scalar() != "0001":
                raise RuntimeError("Run homeline migrate before starting")
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()
            database.engine.dispose()

    app = FastAPI(title="HomeLine Server", version="0.1.0", lifespan=lifespan)
    app.state.service, app.state.runtime = service, runtime
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse(
            {"code": exc.code, "message": exc.code},
            status_code=exc.status,
            headers={
                "Cache-Control": "no-store",
                **({"Retry-After": "60"} if exc.status == 429 else {}),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        return JSONResponse(
            {"code": "invalid_input", "fields": [list(e["loc"]) for e in exc.errors()]},
            status_code=422,
        )

    @app.exception_handler(IntegrityError)
    async def conflict_handler(request, exc):
        return JSONResponse(
            {"code": "constraint_conflict", "message": "Resource conflicts with existing state"},
            status_code=409,
        )

    @app.middleware("http")
    async def guard(request, call_next):
        # Cache bounded bytes; Starlette replays this body to the route.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return JSONResponse({"code": "request_too_large"}, status_code=413)
        request._body = bytes(body)
        try:
            service.rate("http:" + request.client.host, 1000)
        except Problem as exc:
            return await problem_handler(request, exc)
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        return result

    def identity(db, request, admin=False):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not 16 <= len(token) <= 128:
            raise Problem(401, "authentication_required")
        who = service.authenticate(db, token)
        if admin and who.user.role != "admin":
            raise Problem(403, "admin_required")
        return who

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        return JSONResponse(
            {"status": "ready" if runtime.ready else "pbx_unavailable"},
            status_code=200 if runtime.ready else 503,
        )

    @app.post("/v1/sessions", response_model=s.SessionResponse, status_code=201)
    def login(data: s.Login, request: Request):
        service.rate("login:" + request.client.host, 20, 300)
        with database.transaction() as db:
            return service.login(db, data)

    @app.get("/v1/session", response_model=s.SessionResponse)
    def session(request: Request):
        with database.transaction(False) as db:
            return service.session_payload(identity(db, request))

    @app.delete("/v1/session")
    async def logout(request: Request):
        with database.transaction() as db:
            who = identity(db, request)
            service.revoke(db, who.device)
        await runtime.provision()
        return {}

    @app.get("/v1/snapshot", response_model=s.SnapshotResponse)
    def snapshot(request: Request):
        with database.transaction() as db:
            return service.snapshot(db, identity(db, request), runtime.ready)

    @app.get("/v1/devices", response_model=list[s.DeviceResponse])
    def devices(request: Request):
        return snapshot(request)["devices"]

    @app.patch("/v1/devices/{device_id}", response_model=s.DeviceResponse)
    def rename(device_id: str, data: s.Rename, request: Request):
        with database.transaction() as db:
            who = identity(db, request)
            d = service.own_device(db, who, device_id)
            d.name = data.name
            audit(db, who.user.id, "device_renamed", d.id)
            return dict(id=d.id, name=d.name, status=d.status, last_seen_at=utc(d.last_seen_at))

    @app.delete("/v1/devices/{device_id}")
    async def revoke(device_id: str, request: Request):
        with database.transaction() as db:
            who = identity(db, request)

            def action():
                service.revoke(db, service.own_device(db, who, device_id))
                return {}

            result = service.idempotent(
                db, who, "revoke:" + device_id, request.headers.get("idempotency-key"), {}, action
            )
        await runtime.provision()
        return result

    @app.put("/v1/devices/current/push-token")
    def push_token(data: s.PushToken, request: Request):
        with database.transaction() as db:
            who = identity(db, request)
            hashed = digest(data.token) if data.token else None
            other = (
                db.scalar(
                    select(m.Device).where(
                        m.Device.push_hash == hashed, m.Device.id != who.device.id
                    )
                )
                if hashed
                else None
            )
            if other:
                raise Problem(409, "push_token_already_bound")
            who.device.push_hash = hashed
            who.device.push_token = vault.encrypt(data.token) if data.token else None
            audit(db, who.user.id, "push_token_updated", who.device.id)
        return {}

    @app.post("/v1/devices/current/sip-credentials", response_model=s.CredentialsResponse)
    async def credentials(request: Request):
        if not runtime.ready:
            raise Problem(503, "pbx_unavailable")
        with database.transaction() as db:
            who = identity(db, request)
            who.device.sip_expires_at = min(
                who.session.expires_at, time.time() + settings.credential_seconds
            )
            result = dict(
                web_socket_url=settings.sip_wss_url,
                uri=f"sip:d{who.device.id}@{settings.sip_domain}",
                authorization_user="d" + who.device.id,
                password=vault.decrypt(who.device.sip_secret),
                expires_at=utc(who.device.sip_expires_at),
                ice_servers=settings.ice_servers,
            )
        await runtime.provision()
        with database.transaction(False) as db:
            identity(db, request)
        return result

    @app.get("/v1/calls/{call_id}", response_model=s.CallResponse)
    def get_call(call_id: str, request: Request):
        with database.transaction(False) as db:
            call, leg = service.own_call(db, identity(db, request), call_id)
            return service.call_payload(db, call, leg)

    @app.post("/v1/calls/resolve", response_model=s.CallResponse)
    async def resolve(data: s.Resolve, request: Request):
        for attempt in range(6):
            with database.transaction(False) as db:
                who = identity(db, request)
                leg = db.scalar(
                    select(m.Leg).where(
                        m.Leg.device_id == who.device.id, m.Leg.sip_call_id == data.sip_call_id
                    )
                )
                if leg:
                    return service.call_payload(db, db.get(m.Call, leg.call_id), leg)
            if attempt < 5:
                await runtime.sync_call_ids()
                await asyncio.sleep(0.1)
        raise Problem(404, "call_not_found")

    @app.post("/v1/calls/{call_id}/device-events")
    def receipt(call_id: str, data: s.Receipt, request: Request):
        with database.transaction() as db:
            who = identity(db, request)

            def action():
                call, leg = service.own_call(db, who, call_id)
                existing = db.scalar(
                    select(m.DeviceEvent).where(
                        m.DeviceEvent.call_id == call_id,
                        m.DeviceEvent.device_id == who.device.id,
                        m.DeviceEvent.stage == data.stage,
                    )
                )
                if not existing:
                    db.add(
                        m.DeviceEvent(call_id=call_id, device_id=who.device.id, stage=data.stage)
                    )
                    audit(db, who.user.id, "device_" + data.stage, call_id)
                if data.stage == "registered" and leg.state == "waiting":
                    command(db, "ring_leg", call_id, who.device.id)
                if data.stage == "media_lost" and who.device.id in {
                    call.origin_device_id,
                    call.winner_device_id,
                }:
                    change(call, "failed")
                    call.failure_reason = "media_lost"
                    command(db, "end_call", call_id)
                return {}

            return service.idempotent(
                db,
                who,
                "receipt:" + call_id,
                request.headers.get("idempotency-key"),
                data.model_dump(),
                action,
            )

    @app.post("/v1/calls/{call_id}/{action}", response_model=s.CallResponse)
    def call_action(call_id: str, action: str, request: Request):
        with database.transaction() as db:
            who = identity(db, request)
            return service.idempotent(
                db,
                who,
                action + ":" + call_id,
                request.headers.get("idempotency-key"),
                {},
                lambda: service.call_action(db, who, call_id, action),
            )

    @app.post("/v1/outbound-tasks", response_model=s.TaskResponse, status_code=201)
    def outbound(data: s.Outbound, request: Request):
        if not settings.fxo_enabled:
            raise Problem(503, "fxo_not_configured")
        with database.transaction() as db:
            who = identity(db, request)

            def action():
                task = m.OutboundTask(
                    user_id=who.user.id, device_id=who.device.id, destination=data.destination
                )
                db.add(task)
                db.flush()
                audit(db, who.user.id, "outbound_requested", task.id)
                return service.task_payload(db, task)

            return service.idempotent(
                db,
                who,
                "outbound",
                request.headers.get("idempotency-key"),
                data.model_dump(),
                action,
            )

    @app.get("/v1/outbound-tasks", response_model=list[s.TaskResponse])
    def tasks(request: Request):
        return snapshot(request)["outbound_tasks"]

    @app.post("/v1/outbound-tasks/{task_id}/cancel")
    def cancel(task_id: str, request: Request):
        with database.transaction() as db:
            who = identity(db, request)

            def action():
                task = db.get(m.OutboundTask, task_id)
                if not task or task.user_id != who.user.id:
                    raise Problem(404, "task_not_found")
                if task.state == "waiting":
                    task.state = "cancelled"
                    task.revision += 1
                    audit(db, who.user.id, "outbound_cancelled", task.id)
                elif task.state != "cancelled":
                    raise Problem(409, "task_already_running")
                return {}

            return service.idempotent(
                db, who, "cancel:" + task_id, request.headers.get("idempotency-key"), {}, action
            )

    @app.get("/v1/admin/users")
    def users(request: Request):
        with database.transaction(False) as db:
            identity(db, request, True)
            return [
                dict(
                    id=u.id,
                    username=u.username,
                    extension=u.extension,
                    role=u.role,
                    status=u.status,
                    device_limit=u.device_limit,
                )
                for u in db.scalars(select(m.User).order_by(m.User.created_at).limit(1000))
            ]

    @app.post("/v1/admin/users", status_code=201)
    def create_user(data: s.CreateUser, request: Request):
        with database.transaction() as db:
            who = identity(db, request, True)
            u = m.User(
                username=data.username,
                password_hash=hasher.hash(data.password),
                extension=data.extension,
                role=data.role,
                device_limit=data.device_limit,
            )
            db.add(u)
            db.flush()
            audit(db, who.user.id, "user_created", u.id)
            return {"id": u.id}

    @app.patch("/v1/admin/users/{user_id}")
    async def update_user(user_id: str, data: s.UpdateUser, request: Request):
        with database.transaction() as db:
            who = identity(db, request, True)
            user = db.get(m.User, user_id)
            if not user:
                raise Problem(404, "user_not_found")
            if data.status == "disabled" and user.role == "admin" and user.status == "active":
                count = db.scalar(
                    select(func.count())
                    .select_from(m.User)
                    .where(m.User.role == "admin", m.User.status == "active")
                )
                if count <= 1:
                    raise Problem(409, "last_admin")
            if data.status is not None:
                user.status = data.status
            if "device_limit" in data.model_fields_set:
                user.device_limit = data.device_limit
            if data.password:
                user.password_hash = hasher.hash(data.password)
            if data.status == "disabled" or data.password:
                for d in db.scalars(select(m.Device).where(m.Device.user_id == user_id)):
                    service.revoke(db, d)
            audit(db, who.user.id, "user_updated", user_id)
        await runtime.provision()
        return {}

    @app.get("/v1/admin/devices")
    def admin_devices(request: Request):
        with database.transaction(False) as db:
            identity(db, request, True)
            return [
                dict(
                    id=d.id,
                    user_id=d.user_id,
                    name=d.name,
                    platform=d.platform,
                    status=d.status,
                    last_seen_at=utc(d.last_seen_at),
                )
                for d in db.scalars(
                    select(m.Device).order_by(m.Device.last_seen_at.desc()).limit(1000)
                )
            ]

    @app.delete("/v1/admin/devices/{device_id}")
    async def admin_revoke(device_id: str, request: Request):
        with database.transaction() as db:
            who = identity(db, request, True)
            device = service.own_device(db, who, device_id, True)
            service.revoke(db, device)
        await runtime.provision()
        return {}

    @app.patch("/v1/admin/outbound-tasks/{task_id}")
    def priority(task_id: str, data: s.Priority, request: Request):
        with database.transaction() as db:
            who = identity(db, request, True)
            task = db.get(m.OutboundTask, task_id)
            if not task:
                raise Problem(404, "task_not_found")
            if task.state != "waiting":
                raise Problem(409, "task_already_running")
            task.priority = data.priority
            task.revision += 1
            audit(db, who.user.id, "queue_priority_updated", task_id)
        return {}

    @app.get("/v1/admin/audit")
    def audits(request: Request):
        with database.transaction(False) as db:
            identity(db, request, True)
            return [
                dict(
                    id=e.id,
                    actor_id=e.actor_id,
                    action=e.action,
                    resource_id=e.resource_id,
                    created_at=utc(e.created_at),
                )
                for e in db.scalars(select(m.Audit).order_by(m.Audit.created_at.desc()).limit(200))
            ]

    @app.get("/v1/inbound-queue", response_model=list[s.CallResponse])
    def inbound_queue(request: Request):
        with database.transaction(False) as db:
            who = identity(db, request)
            rows = db.execute(
                select(m.Call, m.Leg)
                .join(m.Leg, m.Leg.call_id == m.Call.id)
                .where(
                    m.Leg.device_id == who.device.id,
                    m.Call.kind == "external",
                    m.Call.state.in_(["waiting", "ringing", "claimed"]),
                )
                .order_by(m.Call.created_at)
            )
            return [service.call_payload(db, call, leg) for call, leg in rows]

    @app.get("/v1/me")
    def me(request: Request):
        with database.transaction(False) as db:
            who = identity(db, request)
            return dict(
                id=who.user.id,
                username=who.user.username,
                extension=who.user.extension,
                account_status=who.user.status,
                device_limit=who.user.device_limit,
                role=who.user.role,
            )

    @app.websocket("/v1/events")
    async def events(socket: WebSocket):
        try:
            service.rate("ws:" + socket.client.host, 30)
            with database.transaction(False) as db:
                identity(db, socket)
            await socket.accept()
            last = None
            while True:
                with database.transaction(False) as db:
                    identity(db, socket)
                    newest = db.scalar(
                        select(m.Audit.id).order_by(m.Audit.created_at.desc(), m.Audit.id).limit(1)
                    )
                if newest != last:
                    await socket.send_json({"type": "snapshot_changed"})
                    last = newest
                try:
                    await asyncio.wait_for(socket.receive_text(), 1)
                    await socket.close(code=1008)
                    break
                except asyncio.TimeoutError:
                    pass
        except Problem:
            await socket.close(code=4401)
        except WebSocketDisconnect:
            pass

    from fastapi.openapi.utils import get_openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["Bearer"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Opaque device session token; never pass in URL",
        }
        for path, operations in schema["paths"].items():
            if not path.startswith("/v1/"):
                continue
            for method, operation in operations.items():
                if not isinstance(operation, dict) or method not in {
                    "get",
                    "post",
                    "patch",
                    "put",
                    "delete",
                }:
                    continue
                if path != "/v1/sessions":
                    operation["security"] = [{"Bearer": []}]
                if (
                    path
                    in {
                        "/v1/calls/{call_id}/{action}",
                        "/v1/calls/{call_id}/device-events",
                        "/v1/outbound-tasks",
                        "/v1/outbound-tasks/{task_id}/cancel",
                    }
                    and method == "post"
                    or path == "/v1/devices/{device_id}"
                    and method == "delete"
                ):
                    operation.setdefault("parameters", []).append(
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string", "minLength": 1, "maxLength": 200},
                        }
                    )
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
    return app
