import os
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from bootstrap_runtime import bootstrap, configure_runtime
from rate_limits import RateLimits
from runtime_store import RuntimeStore
from verify_runtime_restore import fingerprint, fresh_database


@pytest.fixture
def shared_limiter():
    password = "ci-runtime-disposable-only-20261113"
    with fresh_database(os.environ["TEST_DATABASE_URL"], closed=False) as url:
        bootstrap(url, password)
        runtime = RuntimeStore(make_conninfo(url, user="meter_api_runtime", password=password))
        yield url, runtime, password


def test_database_quota_is_shared_across_instances_and_survives_reprovision(shared_limiter):
    url, first, password = shared_limiter
    _, token = first.scoped().create_tenant()
    _, other = first.scoped().create_tenant()
    second = RuntimeStore(first.runtime_url)
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(
            pool.map(lambda i: (first if i % 2 else second).admit(token, "same-peer"), range(160))
        )
    assert sum(accepted) == 120
    assert second.admit(other, "same-peer")
    before = fingerprint(url)
    configure_runtime(url, password)
    assert fingerprint(url) == before
    assert not RuntimeStore(first.runtime_url).admit(token, "new-peer")
    with psycopg.connect(url) as conn:
        assert (
            conn.execute("SELECT max(cardinality(hits)) FROM meter_api.admission_windows").fetchone()[0]
            == 120
        )


def test_private_window_capacity_expiry_and_sliding_boundary(shared_limiter):
    url, runtime, _ = shared_limiter
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda i: runtime.admit(None, str(i)), range(256)))
    assert not runtime.admit(None, "new-peer")
    _, token = runtime.scoped().create_tenant()
    assert runtime.admit(token, "new-peer")  # anonymous capacity cannot consume account slots
    with psycopg.connect(url) as conn:
        conn.execute(
            "UPDATE meter_api.admission_windows SET last_seen=clock_timestamp()-interval '61 seconds' WHERE kind='anonymous'"
        )
    assert runtime.admit(None, "new-peer")
    with psycopg.connect(url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM meter_api.admission_windows WHERE kind='anonymous'"
            ).fetchone()[0]
            == 1
        )
        conn.execute(
            "UPDATE meter_api.admission_windows SET hits=array_fill(clock_timestamp()-interval '10 seconds',ARRAY[119]) || ARRAY[clock_timestamp()-interval '61 seconds'] WHERE kind='account'"
        )
    assert runtime.admit(token, "another-peer")
    assert not runtime.admit(token, "another-peer")


@pytest.mark.parametrize("drift", ["SELECT", "SELECT(hits)"])
def test_runtime_cannot_read_reset_or_modify_shared_allowances(shared_limiter, drift):
    url, runtime, _ = shared_limiter
    for statement in (
        "SELECT * FROM meter_api.admission_windows",
        "DELETE FROM meter_api.admission_windows",
        "TRUNCATE meter_api.admission_windows",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), runtime.connection() as conn:
            conn.execute(statement)
    with psycopg.connect(url) as conn:
        conn.execute(f"GRANT {drift} ON meter_api.admission_windows TO meter_api_runtime")
    try:
        with pytest.raises(ValueError, match="admission state"):
            RuntimeStore(runtime.runtime_url)
    finally:
        with psycopg.connect(url) as conn:
            conn.execute(f"REVOKE {drift} ON meter_api.admission_windows FROM meter_api_runtime")


def test_one_account_does_not_spend_another_allowance():
    clock = [0.0]
    limiter = RateLimits(clock=lambda: clock[0], account_limit=3)
    assert [limiter.admit("account", "a") for _ in range(4)] == [True, True, True, False]
    assert limiter.admit("account", "b")
    clock[0] = 59.99
    assert not limiter.admit("account", "a")
    clock[0] = 60.0
    assert limiter.admit("account", "a")


def test_anonymous_capacity_cannot_consume_account_slots_or_evict_quota():
    limiter = RateLimits(maximum_buckets=2, anonymous_limit=1)
    assert limiter.admit("anonymous", "one")
    assert limiter.admit("anonymous", "two")
    assert not limiter.admit("anonymous", "three")
    assert not limiter.admit("anonymous", "one")
    assert limiter.admit("account", "a")
    assert len(limiter.buckets) == 3


def test_concurrent_admission_cannot_overspend_the_allowance():
    limiter = RateLimits(account_limit=10)
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda _: limiter.admit("account", "a"), range(100)))
    assert sum(accepted) == 10


def test_expired_slots_are_reclaimed_without_unbounded_identity_storage():
    clock = [0.0]
    limiter = RateLimits(clock=lambda: clock[0], maximum_buckets=2)
    assert limiter.admit("account", "one")
    assert limiter.admit("account", "two")
    for i in range(1000):
        assert not limiter.admit("account", str(i))
    assert len(limiter.buckets) == 2
    clock[0] = 60.0
    assert limiter.admit("account", "new")
    assert list(limiter.buckets) == [("account", "new")]
