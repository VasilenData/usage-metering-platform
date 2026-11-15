"""Small standard-library helpers for private, local starter state."""

import csv
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path


class StarterError(Exception):
    pass


def require_ignored(root, name):
    for filename in (".gitignore", ".dockerignore"):
        path = plain_path(Path(root) / filename)
        if not path.is_file():
            raise StarterError(
                "Use a full Meterwork checkout with Git and Docker exclusions for private state."
            )
        rules = path.read_text(encoding="utf-8").splitlines()
        if name not in {rule.rstrip("/") for rule in rules} or any(rule.startswith("!") for rule in rules):
            raise StarterError(
                f"{name} must be excluded by both .gitignore and .dockerignore without negation rules."
            )


def plain_path(path):
    path = Path(path).absolute()
    for item in [path, *path.parents]:
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise StarterError("Refusing a symlink or junction in the local state path.")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise StarterError("Refusing a hard-linked local state file.")
    return path


def restrict(path):
    if os.name == "nt":
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
        )
        sid = next(csv.reader([identity.stdout.strip()]))[1]
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise StarterError("Could not restrict the local file ACL; no credentials were written.")
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def private_dir(path):
    path = plain_path(path)
    if not path.exists():
        path.mkdir(mode=0o700)
        restrict(path)
    if not path.is_dir():
        raise StarterError("Local state path must be a directory.")
    check_mode(path)
    return path


def exclusive(path, text):
    path = plain_path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        restrict(path)  # On Windows restrict the empty file before writing secrets.
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_json(path):
    path = plain_path(path)
    check_mode(path)
    if path.stat().st_size > 1_000_000:
        raise StarterError("Local state is too large; keep the original and use a new workspace.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise StarterError("Local state is invalid JSON; keep the original and use a new workspace.") from exc


def atomic_json(path, value):
    path = plain_path(path)
    data = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    os.close(descriptor)
    pending = Path(temporary)
    try:
        restrict(pending)
        with pending.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        plain_path(path)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def check_mode(path):
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise StarterError(
            "Private path allows other users; restrict to current-user access "
            "(chmod 700 for directories, chmod 600 for files) and retry."
        )
