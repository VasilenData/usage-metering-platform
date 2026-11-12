from copy import deepcopy
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from billing import rate


def event(identity, units, at="2026-10-01T00:00:00Z", correction=None):
    return {
        "tenant_id": "a",
        "source": "api",
        "id": identity,
        "units": units,
        "occurred_at": at,
        "correction_of": correction,
    }


PRICES = [
    {"tenant_id": "a", "id": "p1", "effective_from": "2026-01-01T00:00:00Z", "unit_price_microusd": 250},
    {"tenant_id": "a", "id": "p2", "effective_from": "2026-10-15T00:00:00Z", "unit_price_microusd": 200},
]


def test_hand_computed_price_boundary_and_correction():
    events = [
        event("before", 100),
        event("boundary", 50, "2026-10-15T00:00:00Z"),
        event("adjust", -20, correction="before"),
    ]
    result = rate(events, PRICES, tenant_id="a", month="2026-10")
    assert result["amount_microusd"] == 30000  # 80 × 250 + 50 × 200
    assert result["total_units"] == 130
    assert result["pricing_version_ids"] == ["p1", "p2"]
    assert result == rate(list(reversed(events)), list(reversed(PRICES)), tenant_id="a", month="2026-10")


@given(st.lists(st.integers(min_value=1, max_value=100000), min_size=1, max_size=40))
def test_amount_matches_independent_integer_oracle(units):
    events = [event(str(i), value) for i, value in enumerate(units)]
    result = rate(events, PRICES, tenant_id="a", month="2026-10")
    assert result["amount_microusd"] == sum(units) * 250
    assert result["total_units"] == sum(units)


def test_outside_month_does_not_revise_statement():
    initial = [event("x", 10)]
    extra = initial + [event("next", 999, "2026-11-01T00:00:00Z")]
    assert rate(initial, PRICES, tenant_id="a", month="2026-10") == rate(
        extra, PRICES, tenant_id="a", month="2026-10"
    )


@pytest.mark.parametrize(
    "events",
    [
        [event("x", -1)],
        [event("x", 10), event("y", -11, correction="x")],
        [event("x", 10), event("x", 10)],
        [event("x", 10), event("y", 1, correction="absent")],
        [event("x", 10), event("y", -1, correction="x"), event("z", 1, correction="y")],
        [event("x", 10), event("y", -1, "2026-10-02T00:00:00Z", correction="x")],
        [event("x", True)],
        [event("x", 0)],
    ],
)
def test_invalid_ledger_cannot_be_rated(events):
    with pytest.raises(ValueError):
        rate(events, PRICES, tenant_id="a", month="2026-10")


def test_cross_tenant_and_missing_price_fail_closed():
    with pytest.raises(ValueError, match="tenant"):
        rate([event("x", 10)], PRICES, tenant_id="other", month="2026-10")
    with pytest.raises(ValueError, match="Unpriced"):
        rate([event("x", 10)], [], tenant_id="a", month="2026-10")


def test_utc_boundary_not_local_calendar_month():
    result = rate([event("x", 5, "2026-10-01T00:30:00+01:00")], PRICES, tenant_id="a", month="2026-10")
    assert result["total_units"] == 0


def test_inputs_are_not_mutated():
    events = [event("x", 10)]
    copy = deepcopy(events)
    rate(events, PRICES, tenant_id="a", month="2026-10")
    assert events == copy


@pytest.mark.parametrize("value", [True, 0.5, -1])
def test_price_must_be_nonnegative_integer(value):
    with pytest.raises(ValueError, match="integer"):
        rate([event("x", 10)], [dict(PRICES[0], unit_price_microusd=value)], tenant_id="a", month="2026-10")


def test_timestamp_requires_offset():
    with pytest.raises(ValueError, match="offset"):
        rate([event("x", 5, datetime(2026, 10, 1))], PRICES, tenant_id="a", month="2026-10")
    assert (
        rate([event("x", 5, datetime(2026, 10, 1, tzinfo=UTC))], PRICES, tenant_id="a", month="2026-10")[
            "total_units"
        ]
        == 5
    )
