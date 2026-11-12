"""Validate the committed public bundle without a running database."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from export_snapshot import verify


@pytest.fixture
def bundle(tmp_path):
    source = Path(__file__).resolve().parents[1] / "web"
    for name in ("demo.json", "sources.json"):
        shutil.copyfile(source / name, tmp_path / name)
    return tmp_path


def load(bundle, name):
    return json.loads((bundle / name).read_text(encoding="utf-8"))


def save(bundle, name, value):
    (bundle / name).write_text(json.dumps(value), encoding="utf-8")


def refresh_bundle_hash(bundle):
    """A checksum is not a signature; ownership and reproduction must still hold."""
    data = load(bundle, "demo.json")
    data["source_bundle_sha256"] = hashlib.sha256((bundle / "sources.json").read_bytes()).hexdigest()
    save(bundle, "demo.json", data)


def test_committed_snapshot_reproduces_without_database(bundle, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = verify(bundle)
    assert result["statements_reproduced"] == 2
    assert result["usage_events"] == 5
    assert result["product_events"] == 162


@pytest.mark.parametrize(
    "path",
    [
        ("metering", "estimated_amount_microusd"),
        ("metering", "total_units"),
        ("statements", 0, "amount_microusd"),
        ("statements", 0, "total_units"),
        ("statements", 0, "source_sha256"),
        ("source_bundle_sha256",),
    ],
)
def test_modified_published_amount_units_or_fingerprint_is_rejected(bundle, path):
    data = load(bundle, "demo.json")
    container = data
    for key in path[:-1]:
        container = container[key]
    original = container[path[-1]]
    container[path[-1]] = original + 1 if isinstance(original, int) else "0" * 64
    save(bundle, "demo.json", data)
    with pytest.raises(AssertionError):
        verify(bundle)


@pytest.mark.parametrize("table", ["usage_events", "prices", "product_events"])
def test_wrong_tenant_cannot_be_hidden_behind_a_recomputed_checksum(bundle, table):
    source = load(bundle, "sources.json")
    source[table][0]["tenant_id"] = "customer-account"
    save(bundle, "sources.json", source)
    refresh_bundle_hash(bundle)
    with pytest.raises(AssertionError):
        verify(bundle)


def test_changed_source_units_fail_even_when_bundle_checksum_is_updated(bundle):
    source = load(bundle, "sources.json")
    original = next(row for row in source["usage_events"] if row["id"] == "call-001")
    original["units"] += 1
    save(bundle, "sources.json", source)
    refresh_bundle_hash(bundle)
    with pytest.raises(AssertionError, match="does not reproduce"):
        verify(bundle)


def test_non_synthetic_properties_cannot_enter_public_source_bundle(bundle):
    source = load(bundle, "sources.json")
    source["product_events"][0]["properties"] = {"synthetic": True, "email": "fixture@example.invalid"}
    save(bundle, "sources.json", source)
    refresh_bundle_hash(bundle)
    with pytest.raises(AssertionError):
        verify(bundle)
