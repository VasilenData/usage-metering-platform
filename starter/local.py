"""Safe local Compose setup; run from a full Meterwork checkout."""

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from starter.private import (
    StarterError,
    check_mode,
    exclusive,
    plain_path,
    private_dir,
    require_ignored,
    restrict,
)

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("POSTGRES_PASSWORD", "METER_RUNTIME_PASSWORD")


def credentials(root=ROOT):
    root = plain_path(root)
    for name in ("compose.yaml", "bootstrap_runtime.py", ".gitignore", ".dockerignore"):
        if not (root / name).is_file():
            raise StarterError(
                "Use a full Meterwork source checkout; required Compose/bootstrap files are missing."
            )
    require_ignored(root, ".env")
    path = plain_path(root / ".env")
    if not path.exists():
        try:
            exclusive(path, "".join(f"{key}={secrets.token_urlsafe(36)}\n" for key in KEYS))
        except FileExistsError:
            pass  # Another setup won; validate its file without replacing it.
    if not path.is_file() or path.stat().st_size > 4096:
        raise StarterError("Existing .env is not a small credential file; preserved without changes.")
    check_mode(path)
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in KEYS or key in values or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", value):
            raise StarterError(
                "Existing .env must contain two separate URL-safe passwords (24–128 characters). Preserved."
            )
        values[key] = value
    if set(values) != set(KEYS) or values[KEYS[0]] == values[KEYS[1]]:
        raise StarterError(
            "Existing .env needs distinct POSTGRES_PASSWORD and METER_RUNTIME_PASSWORD. Preserved."
        )
    return path


def doctor():
    if sys.version_info < (3, 12):
        raise StarterError("Install Python 3.12 or newer; use python3 on macOS/Linux or py -3.12 on Windows.")
    if not shutil.which("docker"):
        raise StarterError(
            "Docker is missing. Install Docker Engine + Compose v2, or Docker Desktop; then start it."
        )
    for args, message in (
        (["docker", "compose", "version"], "Docker Compose v2 is missing. Install the Compose plugin."),
        (["docker", "info"], "Docker is unavailable. Start Docker and check your local Docker permissions."),
    ):
        try:
            result = subprocess.run(args, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StarterError(message) from exc
        if result.returncode:
            raise StarterError(message)


def compose(action):
    doctor()
    credentials()
    if any(key in os.environ for key in KEYS):
        raise StarterError(
            "Unset exported POSTGRES_PASSWORD and METER_RUNTIME_PASSWORD so Compose uses the saved .env."
        )
    if action == "backup":
        require_ignored(ROOT, ".meterwork")
        folder = private_dir(private_dir(ROOT / ".meterwork") / "backups")
        target = folder / (
            "meterwork-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4) + ".dump"
        )
        pending = target.with_suffix(".pending")
        exclusive(pending, "")
        try:
            with pending.open("wb") as stream:
                result = subprocess.run(
                    [
                        "docker",
                        "compose",
                        "--env-file",
                        str(ROOT / ".env"),
                        "exec",
                        "-T",
                        "postgres",
                        "pg_dump",
                        "-U",
                        "postgres",
                        "-d",
                        "meterline",
                        "--format=custom",
                    ],
                    cwd=ROOT,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            with pending.open("rb") as stream:
                valid = stream.read(5) == b"PGDMP"
            if result.returncode or not valid:
                raise StarterError(
                    "Backup failed; no completed backup published. Check the local PostgreSQL service."
                )
            pending.rename(target)
            restrict(target)
            print("Private PostgreSQL backup: " + str(target))
        finally:
            pending.unlink(missing_ok=True)
        return
    commands = {"up": ["up", "--build", "--detach"], "stop": ["stop"], "start": ["start"]}
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(ROOT / ".env"), *commands[action]],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        raise StarterError(
            "Compose failed. Check docker compose ps and private local logs; saved credentials/data remain."
        )
    if action != "stop":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(90):
            try:
                with opener.open("http://127.0.0.1:8000/healthz", timeout=2) as response:
                    if response.status == 200:
                        print("Ready: http://127.0.0.1:8000 — run python -m starter.client")
                        return
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(1)
        raise StarterError(
            "API did not become healthy. Check port 8000 and docker compose ps; do not reset data."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor", "setup", "up", "stop", "start", "backup"])
    args = parser.parse_args()
    try:
        if args.action == "doctor":
            doctor()
            print("Python, Docker Compose and Docker daemon are available.")
        elif args.action == "setup":
            credentials()
            print("Local .env is ready; existing credentials are preserved and values are never printed.")
        else:
            compose(args.action)
    except (StarterError, OSError, UnicodeError) as exc:
        print(f"Setup stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
