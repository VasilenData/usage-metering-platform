"""First-use safety, recovery and downloadable-source regression tests."""

import http.client
import io
import json
import os
import stat
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from starter import client, local, private
from starter.package import FILES, archive


@pytest.fixture
def checkout(tmp_path):
    for name in ("compose.yaml", "bootstrap_runtime.py"):
        (tmp_path / name).write_text("fixture")
    for name in (".gitignore", ".dockerignore"):
        (tmp_path / name).write_text(".env\n.meterwork/\n")
    return tmp_path


def test_setup_is_private_distinct_and_byte_identical_on_repeat(checkout, capsys):
    path = local.credentials(checkout)
    before = path.read_bytes()
    local.credentials(checkout)
    assert path.read_bytes() == before
    passwords = [line.split("=", 1)[1] for line in before.decode().splitlines()]
    assert passwords[0] != passwords[1] and all(len(value) >= 24 for value in passwords)
    assert not capsys.readouterr().out
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        # Newly created Windows secrets must not retain inherited ACL entries.
        result = local.subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True)
        assert "(I)" not in result.stdout


@pytest.mark.parametrize("content", ["", "POSTGRES_PASSWORD=short\n", "x=y\n", "invalid secret text\n"])
def test_invalid_existing_credentials_are_preserved(checkout, content):
    path = checkout / ".env"
    private.exclusive(path, content)
    before = path.read_bytes()
    with pytest.raises(private.StarterError, match="Existing .env"):
        local.credentials(checkout)
    assert path.read_bytes() == before


def test_credentials_need_distinct_values(checkout):
    value = "a" * 40
    private.exclusive(checkout / ".env", f"POSTGRES_PASSWORD={value}\nMETER_RUNTIME_PASSWORD={value}\n")
    with pytest.raises(private.StarterError, match="distinct"):
        local.credentials(checkout)


def test_setup_refuses_unignored_secret(checkout):
    (checkout / ".dockerignore").write_text("")
    with pytest.raises(private.StarterError, match="excluded"):
        local.credentials(checkout)
    assert not (checkout / ".env").exists()


def test_setup_refuses_negated_secret_exclusion(checkout):
    (checkout / ".gitignore").write_text(".env\n!.env\n")
    with pytest.raises(private.StarterError, match="negation"):
        local.credentials(checkout)
    assert not (checkout / ".env").exists()


def test_client_refuses_unignored_private_state(tmp_path):
    with pytest.raises(private.StarterError, match="exclusions"):
        client.run(root=tmp_path)
    assert not (tmp_path / ".meterwork").exists()


def test_credential_creation_does_not_write_before_permission_success(checkout, monkeypatch):
    def fail(_):
        raise private.StarterError("permissions failed")

    monkeypatch.setattr(private, "restrict", fail)
    with pytest.raises(private.StarterError, match="permissions"):
        local.credentials(checkout)
    assert (checkout / ".env").read_bytes() == b""


def test_hardlink_cannot_redirect_setup(checkout):
    original = checkout / "original"
    original.write_text("unchanged")
    os.link(original, checkout / ".env")
    with pytest.raises(private.StarterError, match="hard-linked"):
        local.credentials(checkout)
    assert original.read_text() == "unchanged"


def test_symlink_path_rejected_even_when_target_absent(tmp_path):
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "absent")
    except OSError:
        pytest.skip("Windows account cannot create symlinks; POSIX CI exercises this case")
    with pytest.raises(private.StarterError, match="symlink"):
        private.plain_path(link)


@pytest.mark.parametrize("workspace", ["../other", "C:\\temp", "/root", ".", "a/b", "a" * 49])
def test_workspace_path_cannot_escape(tmp_path, workspace):
    with pytest.raises(private.StarterError, match="workspace"):
        client.run(workspace=workspace, root=tmp_path)
    assert not (tmp_path / ".meterwork").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost@evil.test",
        "http://127.0.0.1/a",
        "http://127.0.0.1?x=1",
        "http://localhost:0",
        "http://localhost:bad",
        "http://x:secret@localhost",
    ],
)
def test_client_only_accepts_uncredentialed_loopback_origins(url):
    with pytest.raises(private.StarterError, match="loopback"):
        client.local_url(url)


def test_missing_docker_has_actionable_error(monkeypatch):
    monkeypatch.setattr(local.shutil, "which", lambda _: None)
    with pytest.raises(private.StarterError, match="Install Docker"):
        local.doctor()


def test_stopped_docker_has_actionable_error(monkeypatch):
    monkeypatch.setattr(local.shutil, "which", lambda _: "/docker")
    monkeypatch.setattr(
        local.subprocess, "run", lambda args, **kwargs: SimpleNamespace(returncode=int(args[-1] == "info"))
    )
    with pytest.raises(private.StarterError, match="Start Docker"):
        local.doctor()


def test_env_override_stops_before_compose(monkeypatch):
    monkeypatch.setattr(local, "doctor", lambda: None)
    monkeypatch.setattr(local, "credentials", lambda: None)
    monkeypatch.setenv("POSTGRES_PASSWORD", "do-not-print")
    with pytest.raises(private.StarterError, match="Unset exported") as error:
        local.compose("up")
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize(
    "failure",
    [ConnectionResetError(104, "Connection reset by peer"), http.client.RemoteDisconnected("API starting")],
)
def test_startup_readiness_recovers_from_transport_failure(monkeypatch, failure, capsys):
    monkeypatch.setattr(local, "doctor", lambda: None)
    monkeypatch.setattr(local, "credentials", lambda: None)
    for key in local.KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(local.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    attempts, delays = [], []

    def open_request(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        if len(attempts) == 1:
            raise failure
        response = io.BytesIO(b'{"status":"ok"}')
        response.status = 200
        return response

    monkeypatch.setattr(
        local.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request)
    )
    monkeypatch.setattr(local.time, "sleep", delays.append)
    local.compose("up")
    assert attempts == [2, 2] and delays == [1]
    assert "Ready:" in capsys.readouterr().out


def test_startup_readiness_remains_bounded_when_resets_continue(monkeypatch):
    monkeypatch.setattr(local, "doctor", lambda: None)
    monkeypatch.setattr(local, "credentials", lambda: None)
    for key in local.KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(local.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    attempts, delays = [], []

    def open_request(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(
        local.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request)
    )
    monkeypatch.setattr(local.time, "sleep", delays.append)
    with pytest.raises(private.StarterError, match="did not become healthy"):
        local.compose("up")
    assert attempts == [2] * 90 and delays == [1] * 90


class Response(io.BytesIO):
    pass


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("lost after commit"), http.client.IncompleteRead(b"partial")]
)
def test_response_loss_retries_identical_payload_and_identity(error):
    sent = []

    def open_request(request, **kwargs):
        sent.append(request.data)
        if len(sent) < 3:
            raise error
        return Response(b'{"accepted":false,"duplicate":true}')

    result = client.Client(
        "http://127.0.0.1:8000", opener=SimpleNamespace(open=open_request), sleep=lambda _: None
    ).request("POST", "/api/usage", client.ORIGINAL)
    assert result["duplicate"] is True
    assert len(sent) == 3 and len(set(sent)) == 1
    assert json.loads(sent[0])["id"] == "starter-original-v1"


def test_sandbox_unknown_is_not_retried():
    sent = []

    def open_request(request, **kwargs):
        sent.append(request)
        raise urllib.error.URLError("lost")

    with pytest.raises(private.StarterError, match="UNKNOWN"):
        client.Client(
            "http://localhost", opener=SimpleNamespace(open=open_request), sleep=lambda _: None
        ).request("POST", "/api/sandbox", retry=False)
    assert len(sent) == 1


def test_interrupted_sandbox_creation_is_not_reprovisioned(checkout, monkeypatch):
    tmp_path = checkout
    folder = private.private_dir(private.private_dir(tmp_path / ".meterwork") / "first-use")
    private.atomic_json(
        folder / "state.json", {"version": 1, "base_url": "http://127.0.0.1:8000", "phase": "provisioning"}
    )
    calls = []
    monkeypatch.setattr(client.Client, "request", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(private.StarterError, match="creation was interrupted"):
        client.run(root=tmp_path)
    assert calls == []


def test_atomic_failure_preserves_previous_output(tmp_path, monkeypatch):
    target = tmp_path / "result.json"
    private.atomic_json(target, {"valid": True})
    before = target.read_bytes()

    def fail(*args):
        raise OSError("injected rename failure")

    monkeypatch.setattr(private.os, "replace", fail)
    with pytest.raises(OSError):
        private.atomic_json(target, {"valid": False})
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_failed_backup_preserves_prior_archive_and_hides_partial_file(checkout, monkeypatch):
    tmp_path = checkout
    private.private_dir(tmp_path / ".meterwork")
    folder = private.private_dir(tmp_path / ".meterwork/backups")
    old = folder / "previous.dump"
    old.write_bytes(b"PGDMPprevious")
    monkeypatch.setattr(local, "ROOT", tmp_path)
    monkeypatch.setattr(local, "doctor", lambda: None)
    monkeypatch.setattr(local, "credentials", lambda: None)
    for key in local.KEYS:
        monkeypatch.delenv(key, raising=False)
    actual_run = local.subprocess.run

    def run_command(args, **kwargs):
        if args[0] != "docker":
            return actual_run(args, **kwargs)
        kwargs["stdout"].write(b"PGDMPpartial")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(local.subprocess, "run", run_command)
    with pytest.raises(private.StarterError, match="Backup failed"):
        local.compose("backup")
    assert list(folder.iterdir()) == [old]
    assert old.read_bytes() == b"PGDMPprevious"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission enforcement; Windows ACL creation tested separately"
)
def test_existing_broad_read_state_directory_is_rejected(checkout):
    tmp_path = checkout
    folder = tmp_path / ".meterwork"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    with pytest.raises(private.StarterError, match="Private path allows other users"):
        client.run(root=tmp_path)


def test_invalid_reconciliation_never_replaces_good_export(checkout, monkeypatch):
    tmp_path = checkout
    folder = private.private_dir(private.private_dir(tmp_path / ".meterwork") / "first-use")
    private.atomic_json(
        folder / "state.json",
        {
            "version": 1,
            "base_url": "http://127.0.0.1:8000",
            "phase": "ready",
            "token": "x" * 40,
            "old_statement": {"revision": 1},
        },
    )
    private.atomic_json(folder / "result.json", {"previous_complete": True})
    before = (folder / "result.json").read_bytes()

    def request(self, method, path, body=None, **kwargs):
        return {"accepted": False, "duplicate": True} if path == "/api/usage" else {}

    monkeypatch.setattr(client.Client, "request", request)
    with pytest.raises(private.StarterError, match="validation"):
        client.run(root=tmp_path)
    assert (folder / "result.json").read_bytes() == before
    assert not (folder / ".lock").exists()


def test_public_download_contains_exact_source_and_no_private_state():
    root = Path(__file__).resolve().parents[1]
    data = (root / "web/start/meterwork-starter.zip").read_bytes()
    assert data == archive()
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        assert set(bundle.namelist()) == {"docs/FIRST_USE.md", *["starter/" + name for name in FILES]}
    sample = json.loads((root / "web/start/sample.json").read_text())
    assert sample == {"month": "2026-10", "original": client.ORIGINAL, "correction": client.CORRECTION}


def test_archive_bytes_do_not_depend_on_zipinfo_host_platform(monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(zipfile.sys, "platform", "win32")
        windows = archive()
        context.setattr(zipfile.sys, "platform", "linux")
        linux = archive()
    assert windows == linux
    with zipfile.ZipFile(io.BytesIO(windows)) as bundle:
        assert all(
            entry.create_system == 3 and entry.external_attr == 0o100644 << 16 for entry in bundle.infolist()
        )
