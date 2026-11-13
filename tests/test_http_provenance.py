import hashlib
import subprocess

import pytest

import verify_http


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL)

    git("init", "--quiet")
    (tmp_path / "app.py").write_bytes(b"# synthetic baseline fixture\n")
    git("add", "app.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    monkeypatch.setattr(verify_http, "BASELINE_COMMIT", git("rev-parse", "HEAD").decode().strip())
    return tmp_path


def test_baseline_records_hashes_of_verified_bytes(baseline):
    directory, hashes = verify_http.verified_baseline(baseline)
    assert directory == baseline.resolve()
    assert hashes == {"app.py": hashlib.sha256((baseline / "app.py").read_bytes()).hexdigest()}


def test_baseline_rejects_wrong_commit(baseline, monkeypatch):
    monkeypatch.setattr(verify_http, "BASELINE_COMMIT", "0" * 40)
    with pytest.raises(ValueError, match="pinned commit"):
        verify_http.verified_baseline(baseline)


def test_baseline_rejects_modified_tracked_bytes(baseline):
    (baseline / "app.py").write_bytes(b"# modified fixture\n")
    with pytest.raises(ValueError, match="pinned bytes"):
        verify_http.verified_baseline(baseline)


def test_baseline_rejects_ignored_python_shadow_module(baseline):
    # Even a globally or repository-ignored source must not shadow a dependency.
    (baseline / ".gitignore").write_text("shadow/\n")
    (baseline / "shadow").mkdir()
    (baseline / "shadow" / "fastapi.py").write_text("# unexpected fixture\n")
    with pytest.raises(ValueError, match="untracked Python"):
        verify_http.verified_baseline(baseline)
