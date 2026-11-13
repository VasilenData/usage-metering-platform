import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from billing import rate, timestamp
from demo import overview, run_scenario, seed, usage
from statement_diff import compare_statements
from store import Conflict, Store
from verify_workload import sql_totals


@pytest.fixture(scope="module")
def store():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required: integration tests must run against PostgreSQL")
    with psycopg.connect(url) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        if not name.endswith("_test"):
            pytest.fail("Integration database must end in _test; refusing to mutate it")
    result = Store(url)
    with result.admin() as conn:
        conn.execute("DELETE FROM tenants")
    seed(result)
    yield result


@pytest.fixture
def tenant(store):
    item, token = store.create_tenant()
    yield item["id"], token
    with store.admin() as conn:
        conn.execute("DELETE FROM tenants WHERE id=%s", (item["id"],))


def test_exact_replay_conflict_and_concurrent_ingestion(store, tenant):
    tenant_id, _ = tenant
    body = usage("same-request", 100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: store.ingest_usage(tenant_id, body), range(12)))
    assert sum(r["accepted"] for r in responses) == 1
    assert sum(r["duplicate"] for r in responses) == 11
    assert len(store.snapshot(tenant_id)[0]) == 1
    with pytest.raises(Conflict):
        store.ingest_usage(tenant_id, dict(body, units=101))


def test_late_correction_and_failed_publication_preserve_last_good(store, tenant):
    tenant_id, _ = tenant
    store.ingest_usage(tenant_id, usage("first", 100))
    original = store.publish(tenant_id, "2026-10")
    store.ingest_usage(tenant_id, usage("late", 40))
    store.ingest_usage(tenant_id, usage("refund", -20, correction_of="first"))

    def crash(conn, document):
        raise RuntimeError("synchronized after INSERT, before COMMIT")

    with pytest.raises(RuntimeError):
        store.publish(tenant_id, "2026-10", before_commit=crash)
    assert store.snapshot(tenant_id)[3] == [original]
    new = store.publish(tenant_id, "2026-10")
    assert new["revision"] == 2 and new["amount_microusd"] == 30000
    assert original in store.snapshot(tenant_id)[3]
    assert store.publish(tenant_id, "2026-10") == new


def test_concurrent_publication_has_one_revision(store, tenant):
    tenant_id, _ = tenant
    store.ingest_usage(tenant_id, usage("x", 10))
    with ThreadPoolExecutor(max_workers=4) as pool:
        documents = list(pool.map(lambda _: store.publish(tenant_id, "2026-10"), range(8)))
    assert len({document["id"] for document in documents}) == 1


def test_backdated_price_preserves_old_revision_and_known_at_reproduction(store, tenant):
    tid = tenant[0]
    store.ingest_usage(tid, usage("original", 100, "2026-10-20T00:00:00Z"))
    before = store.publish(tid, "2026-10")
    price = store.provision_price(
        tid, price_id="negotiated-v3", effective_from="2026-10-18T00:00:00Z", unit_price_microusd=150
    )
    assert price["recorded_at"] > timestamp(before["known_at"])
    assert (
        store.provision_price(
            tid, price_id="negotiated-v3", effective_from="2026-10-18T00:00:00Z", unit_price_microusd=150
        )
        == price
    )
    with pytest.raises(Conflict):
        store.provision_price(
            tid, price_id="negotiated-v3", effective_from="2026-10-18T00:00:00Z", unit_price_microusd=151
        )
    with pytest.raises(Conflict):
        store.provision_price(
            tid, price_id="ambiguous", effective_from="2026-10-18T00:00:00Z", unit_price_microusd=150
        )
    after = store.publish(tid, "2026-10")
    events, prices, _, statements = store.snapshot(tid)
    assert before in statements
    assert before["amount_microusd"] == 20000 and after["amount_microusd"] == 15000
    for document in statements:
        reproduced = rate(events, prices, tenant_id=tid, month="2026-10", known_at=document["known_at"])
        assert reproduced["content_sha256"] == document["content_sha256"]
    bridge = compare_statements(before, after)
    assert bridge["summary"]["price_change"]["delta_amount_microusd"] == -5000
    assert store.publish(tid, "2026-10") == after


def test_price_administration_serializes_with_publication(store, tenant):
    tid = tenant[0]
    store.ingest_usage(tid, usage("original", 100, "2026-10-20T00:00:00Z"))
    inserted, release, price_started = threading.Event(), threading.Event(), threading.Event()

    def boundary(conn, document):
        inserted.set()
        assert release.wait(10)

    def provision():
        price_started.set()
        return store.provision_price(
            tid, price_id="concurrent-v3", effective_from="2026-10-18T00:00:00Z", unit_price_microusd=150
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(store.publish, tid, "2026-10", before_commit=boundary)
        assert inserted.wait(10)
        pricing = pool.submit(provision)
        assert price_started.wait(10)
        release.set()
        before, price = publishing.result(), pricing.result()
    after = store.publish(tid, "2026-10")
    assert price["recorded_at"] > timestamp(before["known_at"])
    assert before["amount_microusd"] == 20000
    assert after["amount_microusd"] == 15000 and after["revision"] == 2


@pytest.mark.parametrize("zone", ["UTC", "Europe/Lisbon", "Pacific/Auckland"])
def test_independent_sql_uses_utc_month_boundaries_across_session_timezones(store, tenant, zone):
    tid = tenant[0]
    for index, occurred in enumerate(
        [
            "2026-09-30T23:59:59Z",
            "2026-10-01T00:00:00Z",
            "2026-10-31T23:59:59Z",
            "2026-11-01T00:00:00Z",
        ],
        start=1,
    ):
        store.ingest_usage(tid, usage(str(index), index, occurred))
    document = store.publish(tid, "2026-10")

    class SessionZoneStore:
        @contextmanager
        def tenant(self, tenant_id):
            with store.tenant(tenant_id) as conn:
                conn.execute("SELECT set_config('TimeZone',%s,true)", (zone,))
                yield conn

    expected = {"events": 2, "units": 5, "amount_microusd": 1100}
    assert sql_totals(SessionZoneStore(), tid, document["known_at"]) == expected
    assert document["total_units"] == 5 and document["amount_microusd"] == 1100


def test_database_rls_blocks_omitted_filter_cross_read_and_write(store, tenant):
    tenant_id, _ = tenant
    store.ingest_usage(tenant_id, usage("mine", 10))
    with store.tenant(tenant_id) as conn:
        rows = conn.execute("SELECT DISTINCT tenant_id FROM usage_events").fetchall()
        assert rows == [{"tenant_id": tenant_id}]
        assert conn.execute("SELECT * FROM usage_events WHERE tenant_id='demo'").fetchall() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with store.tenant(tenant_id) as conn:
            conn.execute(
                "INSERT INTO usage_events(tenant_id,source,id,occurred_at,units,payload_sha256) VALUES('demo','x','intrusion',now(),1,'x')"
            )
    with psycopg.connect(store.runtime_url) as conn:
        assert conn.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 0
        role = conn.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert role == (False, False)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM usage_events",
        "UPDATE usage_events SET units=0",
        "DELETE FROM statements",
        "INSERT INTO prices VALUES('demo','bad',now(),0)",
    ],
)
def test_request_identity_cannot_mutate_history_or_prices(store, tenant, sql):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with store.tenant(tenant[0]) as conn:
            conn.execute(sql)


def test_overcorrection_is_rejected_even_when_concurrent(store, tenant):
    tenant_id, _ = tenant
    store.ingest_usage(tenant_id, usage("original", 100))

    def adjust(i):
        try:
            store.ingest_usage(tenant_id, usage(f"correction-{i}", -60, correction_of="original"))
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(adjust, range(2))) == 1
    assert store.publish(tenant_id, "2026-10")["total_units"] == 40


def test_readers_only_observe_committed_statements(store, tenant):
    tenant_id, _ = tenant
    store.ingest_usage(tenant_id, usage("first", 100))
    original = store.publish(tenant_id, "2026-10")
    store.ingest_usage(tenant_id, usage("late", 10))
    inserted = threading.Event()
    release = threading.Event()

    def boundary(conn, document):
        inserted.set()
        assert release.wait(10)
        raise RuntimeError("interrupted")

    with ThreadPoolExecutor() as pool:
        writer = pool.submit(store.publish, tenant_id, "2026-10", before_commit=boundary)
        assert inserted.wait(10)
        # Independent reader intentionally doesn't acquire application advisory lock.
        with psycopg.connect(store.runtime_url) as conn:
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (tenant_id,))
            assert conn.execute("SELECT document FROM statements").fetchall() == [(original,)]
        release.set()
        with pytest.raises(RuntimeError):
            writer.result()


@pytest.mark.parametrize("scenario", ["replay", "late", "pricing", "isolation"])
def test_public_exercises_execute_real_engine(store, scenario):
    result = run_scenario(store, scenario)
    assert result["checks"] and all(row["passed"] for row in result["checks"])


def test_seed_overview_is_explicitly_synthetic(store):
    result = overview(store, "demo")
    assert result["scope"] == "synthetic-demo"
    assert result["metering"]["total_units"] == 28400
    assert len(result["statements"]) == 2


def test_interrupted_seed_resumes_and_completed_seed_does_not_change_history(store, monkeypatch):
    with store.admin() as conn:
        conn.execute("DELETE FROM tenants WHERE id='demo'")
    ingest = store.ingest_product
    counter = 0

    def interrupted(*args, **kwargs):
        nonlocal counter
        counter += 1
        if counter == 8:
            raise RuntimeError("interrupted seed after committed events")
        return ingest(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "ingest_product", interrupted)
        with pytest.raises(RuntimeError):
            seed(store)
    seed(store)
    original = store.snapshot("demo")
    assert len(original[0]) == 5 and len(original[2]) == 162 and len(original[3]) == 2
    seed(store)
    assert store.snapshot("demo") == original


def test_http_contract_auth_and_input_validation(store, tenant):
    from psycopg.conninfo import make_conninfo

    import app
    from bootstrap_runtime import bootstrap
    from runtime_store import RuntimeStore

    password = "ci-runtime-disposable-only-20261113"
    bootstrap(store.admin_url, password)
    app.store = RuntimeStore(make_conninfo(store.admin_url, user="meter_api_runtime", password=password))
    app.limits.clear()
    client = TestClient(app.app)
    token = tenant[1]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert (
        client.post("/api/usage", json=dict(usage("one", 10), tenant_id="demo"), headers=headers).status_code
        == 422
    )
    assert client.post("/api/usage", json=usage("one", True), headers=headers).status_code == 422
    assert client.post("/api/usage", json=usage("one", 10), headers=headers).status_code == 200
    assert client.post("/api/usage", json=usage("one", 11), headers=headers).status_code == 409
    assert (
        client.post("/api/usage", json=usage("two", 10, "2026-10-01T00:00:00"), headers=headers).status_code
        == 422
    )
    response = client.get("/api/overview", headers=headers)
    assert response.json()["tenant"]["id"] == tenant[0]
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert client.post("/api/usage", content="x" * 9000, headers=headers).status_code == 413
    assigned = client.post("/api/assignment", json={"actor_id": "visitor"}, headers=headers)
    assert assigned.status_code == 200
    assert (
        client.post("/api/assignment", json={"actor_id": "visitor"}, headers=headers).json()
        == assigned.json()
    )
    assert (
        client.post(
            "/api/events",
            json={
                "event_id": "made-up",
                "actor_id": "visitor",
                "event_name": "assignment",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
            headers=headers,
        ).status_code
        == 422
    )
