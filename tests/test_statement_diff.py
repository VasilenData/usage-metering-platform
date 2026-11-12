from copy import deepcopy

import pytest

from billing import digest, rate
from statement_diff import compare_statements


def usage(identity, units, *, correction=None, received="2026-11-01T00:00:00Z"):
    return {
        "tenant_id": "one",
        "source": "api",
        "id": identity,
        "units": units,
        "occurred_at": "2026-10-20T00:00:00Z",
        "received_at": received,
        "correction_of": correction,
    }


PRICES = [
    {
        "tenant_id": "one",
        "id": "p1",
        "effective_from": "2026-01-01T00:00:00Z",
        "unit_price_microusd": 250,
        "recorded_at": "2026-10-01T00:00:00Z",
    },
    {
        "tenant_id": "one",
        "id": "p2",
        "effective_from": "2026-10-15T00:00:00Z",
        "unit_price_microusd": 200,
        "recorded_at": "2026-11-03T00:00:00Z",
    },
]


def document(events, revision, cutoff):
    result = rate(events, PRICES, tenant_id="one", month="2026-10", known_at=cutoff)
    return dict(result, id=str(revision), revision=revision, created_at=cutoff, known_at=cutoff)


def pair():
    initial = usage("original", 100)
    late = usage("late", 40, received="2026-11-03T01:00:00Z")
    correction = usage("correction", -20, correction="original", received="2026-11-03T01:00:00Z")
    return (
        document([initial], 1, "2026-11-02T00:00:00Z"),
        document([initial, late, correction], 2, "2026-11-04T00:00:00Z"),
    )


def test_backdated_price_and_late_events_do_not_rewrite_known_at_history():
    before, after = pair()
    assert before["amount_microusd"] == 25000
    assert after["amount_microusd"] == 24000
    all_events = [usage("original", 100), usage("late", 40, received="2026-11-03T01:00:00Z")]
    reproduced = document(all_events, 1, before["known_at"])
    assert reproduced == before


def test_bridge_is_hand_reconciled_across_simultaneous_causes():
    before, after = pair()
    result = compare_statements(before, after)
    assert result["delta_units"] == 20
    assert result["delta_amount_microusd"] == -1000
    assert result["summary"] == {
        "late_usage": {"lines": 1, "delta_units": 40, "delta_amount_microusd": 8000},
        "usage_correction": {"lines": 1, "delta_units": -20, "delta_amount_microusd": -4000},
        "price_change": {"lines": 1, "delta_units": 0, "delta_amount_microusd": -5000},
    }


@pytest.mark.parametrize("field,value", [("tenant_id", "other"), ("month", "2026-09"), ("currency", "EUR")])
def test_cross_scope_comparison_fails(field, value):
    before, after = pair()
    after[field] = value
    with pytest.raises(ValueError, match=field):
        compare_statements(before, after)


def test_reverse_revision_and_tampering_are_rejected():
    before, after = pair()
    with pytest.raises(ValueError, match="forward"):
        compare_statements(after, before)
    after["lines"][0]["amount_microusd"] += 1
    with pytest.raises(ValueError, match="fingerprint"):
        compare_statements(before, after)


def test_even_rehashed_broken_line_amount_fails_reconciliation():
    before, after = pair()
    after["lines"][0]["amount_microusd"] += 1
    after["content_sha256"] = digest(
        {
            k: v
            for k, v in after.items()
            if k not in {"id", "revision", "created_at", "known_at", "content_sha256"}
        }
    )
    with pytest.raises(ValueError, match="line amount"):
        compare_statements(before, after)


def test_comparison_is_pure():
    before, after = pair()
    original = deepcopy((before, after))
    compare_statements(before, after)
    assert (before, after) == original


def test_future_cross_tenant_price_cannot_hide_behind_cutoff():
    prices = [*PRICES, {**PRICES[0], "tenant_id": "two", "recorded_at": "2099-01-01T00:00:00Z"}]
    with pytest.raises(ValueError, match="tenant"):
        rate([usage("a", 10)], prices, tenant_id="one", month="2026-10", known_at="2026-11-02T00:00:00Z")


def test_legacy_engine_price_fingerprint_survives_receipt_column_migration():
    prices = [{k: v for k, v in p.items() if k != "recorded_at"} for p in PRICES[:1]]
    old = rate([usage("a", 10)], prices, tenant_id="one", month="2026-10", engine_version="1.0.0")
    new = rate(
        [usage("a", 10)],
        PRICES,
        tenant_id="one",
        month="2026-10",
        known_at="2026-11-02T00:00:00Z",
        engine_version="1.0.0",
    )
    assert new == old


@pytest.mark.parametrize("field", ["received_at", "recorded_at"])
@pytest.mark.parametrize("failure", ["missing", "null", "naive"])
def test_explicit_new_engine_cutoff_requires_receipt_clocks(field, failure):
    events, prices = [usage("a", 100)], deepcopy(PRICES)
    row = events[0] if field == "received_at" else prices[0]
    if failure == "missing":
        del row[field]
    else:
        row[field] = None if failure == "null" else "2026-10-01T00:00:00"
    with pytest.raises(ValueError, match=field):
        rate(events, prices, tenant_id="one", month="2026-10", known_at="2026-11-04T00:00:00Z")


def test_legacy_known_at_snapshot_accepts_original_price_schema():
    prices = [{k: v for k, v in PRICES[0].items() if k != "recorded_at"}]
    expected = rate([usage("a", 100)], prices, tenant_id="one", month="2026-10", engine_version="1.0.0")
    actual = rate(
        [usage("a", 100)],
        prices,
        tenant_id="one",
        month="2026-10",
        known_at="2026-11-04T00:00:00Z",
        engine_version="1.0.0",
    )
    assert actual == expected
