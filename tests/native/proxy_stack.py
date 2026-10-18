# SPDX-License-Identifier: MPL-2.0
"""Real deployment-template Nginx + Uvicorn, isolated to unprivileged test ports."""

import asyncio
import importlib.util
import os
import ssl
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn


@asynccontextmanager
async def api_client(app, root, proxy):
    if not proxy:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client, 18089
        return
    executable = os.environ["HOMELINE_TEST_NGINX"]
    template = Path(__file__).resolve().parents[2] / "deploy/configure.py"
    spec = importlib.util.spec_from_file_location("proxy_deployment_config", template)
    configure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(configure)
    configure.configure(
        root / "rendered", "localhost", str(root / "tls.pem"), str(root / "tls.key")
    )
    server = (root / "rendered/etc/nginx/sites-available/homeline").read_text()
    server = (
        server.replace("listen 443 ssl;", "listen 127.0.0.1:18443 ssl;")
        .replace("127.0.0.1:8089", "127.0.0.1:18089")
        .replace("127.0.0.1:8000", "127.0.0.1:18000")
        .replace("/etc/ssl/certs/ca-certificates.crt", str(root / "tls.pem"))
    )
    bad_server = server.replace(":18443 ssl;", ":18444 ssl;").replace(
        "proxy_ssl_name localhost;", "proxy_ssl_name wrong.example;"
    )
    config = root / "nginx.conf"
    config.write_text(
        f"worker_processes 1;\nerror_log {root}/nginx.log notice;\npid {root}/nginx.pid;\nevents {{ worker_connections 128; }}\nhttp {{ client_body_temp_path {root}/body; proxy_temp_path {root}/proxy;\n"
        + server
        + bad_server
        + "\n}\n"
    )
    subprocess.run(
        [executable, "-t", "-p", str(root) + "/", "-c", str(config)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    server_app = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=18000,
            lifespan="off",
            access_log=False,
            proxy_headers=False,
            log_level="warning",
        )
    )
    task = asyncio.create_task(server_app.serve())
    nginx = None
    output = (root / "nginx-process.log").open("w")
    try:
        for _ in range(300):
            if server_app.started:
                break
            if task.done():
                await task
                raise RuntimeError("Uvicorn exited before binding")
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Uvicorn startup timed out")
        nginx = subprocess.Popen(
            [executable, "-p", str(root) + "/", "-c", str(config), "-g", "daemon off;"],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        context = ssl.create_default_context(cafile=str(root / "tls.pem"))
        async with httpx.AsyncClient(
            base_url="https://localhost:18443", verify=context, trust_env=False, timeout=10
        ) as client:
            for _ in range(300):
                try:
                    response = await client.get("/health/ready")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if nginx.poll() is not None:
                    raise RuntimeError("Nginx exited; inspect nginx.log")
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Nginx readiness timed out")
            assert (await client.get("/ari/asterisk/info")).status_code == 404
            yield client, 18443
    finally:
        if nginx and nginx.poll() is None:
            nginx.terminate()
            try:
                await asyncio.to_thread(nginx.wait, 5)
            except subprocess.TimeoutExpired:
                nginx.kill()
                await asyncio.to_thread(nginx.wait)
        server_app.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        output.close()
