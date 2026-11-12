import os
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

import app
from store import Store


@pytest.fixture(scope="module")
def http_store():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for HTTP integration tests")
    with psycopg.connect(url) as conn:
        if not conn.execute("SELECT current_database()").fetchone()[0].endswith("_test"):
            pytest.fail("Integration database must end in _test")
    return Store(url)


def test_concurrent_first_assignment_is_idempotent(http_store, monkeypatch):
    tenant, token = http_store.create_tenant()
    monkeypatch.setattr(app, "store", http_store)
    app.request_times.clear()
    barrier = threading.Barrier(6)
    ingest = http_store.ingest_product

    def synchronized(tenant_id, body):
        # Every request has observed no prior assignment before any insert occurs.
        barrier.wait(10)
        return ingest(tenant_id, body)

    monkeypatch.setattr(http_store, "ingest_product", synchronized)

    def request(_):
        client = TestClient(app.app)
        return client.post(
            "/api/assignment", json={"actor_id": "same-actor"}, headers={"Authorization": f"Bearer {token}"}
        )

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(request, range(6)))
        assert [r.status_code for r in responses] == [200] * 6
        assert all(r.json() == responses[0].json() for r in responses)
        assert len(http_store.snapshot(tenant["id"])[2]) == 1
    finally:
        with http_store.admin() as conn:
            conn.execute("DELETE FROM tenants WHERE id=%s", (tenant["id"],))


def test_chunked_request_without_length_is_still_bounded():
    app.request_times.clear()
    client = TestClient(app.app)
    chunks = (b"x" * 4096 for _ in range(3))
    response = client.post("/api/sandbox", content=chunks)
    assert response.status_code == 413
    assert response.json()["detail"] == "Request exceeds 8KB"


def test_openapi_contract_is_available_without_external_scripts():
    client = TestClient(app.app)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/assignment" in response.json()["paths"]
    assert "default-src 'self'" in response.headers["content-security-policy"]
