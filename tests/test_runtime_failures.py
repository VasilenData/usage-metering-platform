from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

import app
from rate_limits import RateLimits
from store import Store
from verify_http import usage


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, "limits", RateLimits())
    return TestClient(app.app)


@pytest.mark.parametrize("reference", ["", " ", "bad/reference", 0, False])
def test_invalid_correction_reference_fails_before_storage(client, monkeypatch, reference):
    class NoWrites:
        def authenticate(self, _):
            return {"id": "account"}

        def scoped(self, *_):
            return self

        def ingest_usage(self, *_):
            pytest.fail("Invalid reference must never reach PostgreSQL")

    monkeypatch.setattr(app, "store", NoWrites())
    response = client.post(
        "/api/usage",
        json=usage("invalid-reference", correction_of=reference),
        headers={"Authorization": "Bearer test"},
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


def test_offline_store_also_rejects_empty_reference_before_opening_a_transaction():
    class NoConnection(Store):
        def __init__(self):
            pass

        @contextmanager
        def tenant(self, _):
            pytest.fail("Invalid reference must fail before connection or insert")
            yield

    with pytest.raises(ValueError, match="nonempty event identifier"):
        NoConnection().ingest_usage("account", usage("invalid", correction_of=""))


@pytest.mark.parametrize(
    "error", [psycopg.OperationalError, psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable]
)
def test_post_admission_database_failure_is_sanitized_and_does_not_claim_rollback(client, monkeypatch, error):
    class InterruptedWrite:
        def authenticate(self, _):
            return {"id": "account"}

        def scoped(self, *_):
            return self

        def ingest_usage(self, *_):
            raise error("private database details must not escape")

    monkeypatch.setattr(app, "store", InterruptedWrite())
    response = client.post("/api/usage", json=usage("same-id"), headers={"Authorization": "Bearer test"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Database operation temporarily unavailable", "outcome": "unknown"}
    assert response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_health_failure_is_not_cacheable(client, monkeypatch):
    class Unavailable:
        def connection(self):
            raise psycopg.OperationalError("private host")

    monkeypatch.setattr(app, "store", Unavailable())
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_programming_errors_are_not_misreported_as_temporary_outages(client, monkeypatch):
    class Broken:
        def connection(self):
            raise RuntimeError("programming fault")

    monkeypatch.setattr(app, "store", Broken())
    with pytest.raises(RuntimeError, match="programming fault"):
        client.get("/healthz")
