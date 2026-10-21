#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Run independently installed acme.sh; DNS secrets never become CLI arguments."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

ACME_COMMIT = "807da6498377ee5e0cf43a78091f46f12dc59a89"  # Upstream 3.1.6, GPL-3.0
HOME = Path("/opt/homeline-acme")
CONFIG = Path("/etc/homeline-acme")
CERTS = Path("/etc/homeline-certs")
HELPERS = Path("/usr/local/libexec")
SYSTEMD = Path("/etc/systemd/system")
SERVER_ENV = Path("/etc/homeline/server.env")
NGINX = Path("/etc/nginx/sites-available/homeline")


def credentials(path):
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("DNS credentials must be a root-owned 0600 JSON file")
    if path.stat().st_size > 32768:
        raise ValueError("DNS credentials file is too large")
    try:
        data = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise ValueError("Invalid DNS credentials JSON") from None
    if (
        not isinstance(data, dict)
        or not data
        or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or not isinstance(value, str)
            or "\0" in value
            or len(value) > 4096
            for key, value in data.items()
        )
    ):
        raise ValueError("DNS credentials must map environment variable names to strings")
    # Prevent a configuration file from overriding shell/library execution behavior.
    if any(
        key in {"PATH", "HOME", "ENV", "BASH_ENV", "SHELLOPTS", "LD_PRELOAD", "LD_LIBRARY_PATH"}
        or key.startswith("BASH_FUNC_")
        for key in data
    ):
        raise ValueError("Reserved environment variable in DNS credentials")
    return data


def run(*args, env=None, cwd=None):
    subprocess.run([str(a) for a in args], check=True, env=env, cwd=cwd)


def require_root():
    if os.geteuid() != 0:
        raise ValueError("ACME setup requires root")


def setup(host, email, dns, env_file):
    require_root()
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,200}", host) or not re.fullmatch(
        r"[^\s@]+@[^\s@]+\.[^\s@]+", email
    ):
        raise ValueError("Provide a DNS hostname and ACME account email")
    if not re.fullmatch(r"dns_[A-Za-z0-9_]+", dns):
        raise ValueError("Provide an acme.sh DNS API plugin name")
    secret_env = credentials(env_file)
    if SERVER_ENV.exists() and (
        not NGINX.exists()
        or "ssl_certificate /etc/homeline-certs/fullchain.pem;" not in NGINX.read_text()
    ):
        raise ValueError(
            "Existing installation uses external certificate paths; migrate those paths before enabling managed ACME"
        )
    for path in (HOME, CONFIG, CERTS):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    # Nginx's root master reads certificates; the PBX receives restricted copies.
    env = dict(os.environ, **secret_env)
    acme = HOME / "acme.sh"
    base = [acme, "--home", HOME, "--config-home", CONFIG]
    if not acme.exists():
        with tempfile.TemporaryDirectory(prefix="homeline-acme-") as directory:
            repo = Path(directory) / "source"
            run("git", "init", "-q", repo)
            run(
                "git",
                "-C",
                repo,
                "remote",
                "add",
                "origin",
                "https://github.com/acmesh-official/acme.sh.git",
            )
            run("git", "-C", repo, "fetch", "--depth=1", "origin", ACME_COMMIT)
            run("git", "-C", repo, "checkout", "--detach", "FETCH_HEAD")
            actual = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            if actual != ACME_COMMIT:
                raise ValueError("Unexpected acme.sh source revision")
            run(
                "sh",
                repo / "acme.sh",
                "--install",
                "--home",
                HOME,
                "--config-home",
                CONFIG,
                "--accountemail",
                email,
                "--nocron",
                "--noprofile",
                cwd=repo,
            )
            shutil.copyfile(repo / "LICENSE.md", HOME / "UPSTREAM-LICENSE.md")
    version = subprocess.check_output([str(acme), "--version"], text=True)
    if "v3.1.6" not in version.splitlines():
        raise ValueError("Unexpected acme.sh version; review before upgrading")
    if not (HOME / "dnsapi" / (dns + ".sh")).is_file():
        raise ValueError("DNS API plugin not present in pinned acme.sh")
    existing = CONFIG / "domain"
    if existing.exists() and existing.read_text().strip() != host:
        raise ValueError("Existing ACME installation belongs to a different hostname")
    existing.write_text(host + "\n")
    existing.chmod(0o600)
    helpers = HELPERS
    helpers.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        Path(__file__).with_name("activate-cert.sh"), helpers / "homeline-activate-cert"
    )
    (helpers / "homeline-activate-cert").chmod(0o700)
    try:
        run(
            *base,
            "--issue",
            "--server",
            "letsencrypt",
            "--dns",
            dns,
            "-d",
            host,
            "--keylength",
            "ec-256",
            "--accountemail",
            email,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 2:  # acme.sh: existing certificate is not due for renewal.
            raise
    run(
        *base,
        "--install-cert",
        "--ecc",
        "-d",
        host,
        "--key-file",
        CERTS / "key.pem",
        "--fullchain-file",
        CERTS / "fullchain.pem",
        "--reloadcmd",
        "touch /etc/homeline-certs/pending; /usr/local/libexec/homeline-activate-cert",
        env=env,
    )
    for path in (CERTS / "key.pem", CERTS / "fullchain.pem"):
        path.chmod(0o600)
    for unit in (
        "homeline-acme.service",
        "homeline-acme.timer",
        "homeline-cert-activate.service",
        "homeline-cert-activate.timer",
    ):
        shutil.copyfile(Path(__file__).with_name("systemd") / unit, SYSTEMD / unit)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "homeline-acme.timer", "homeline-cert-activate.timer")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--dns", required=True)
    parser.add_argument("--dns-env", type=Path, required=True)
    args = parser.parse_args()
    try:
        setup(args.host, args.email, args.dns, args.dns_env)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Neither configuration values nor subprocess argument details are printed here.
        raise SystemExit(
            "ACME setup failed: " + type(exc).__name__ + "; inspect acme.sh status output"
        ) from None
