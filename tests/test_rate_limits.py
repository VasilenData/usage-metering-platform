from concurrent.futures import ThreadPoolExecutor

from rate_limits import RateLimits


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
