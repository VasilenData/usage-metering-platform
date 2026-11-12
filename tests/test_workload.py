import pytest

from verify_workload import expected_totals, fixture, quantile


def test_independent_oracle_hand_calculated_price_and_month_boundaries():
    rows = [
        {"occurred_at": date, "units": units}
        for date, units in [
            ("2026-09-30T23:59:59Z", 999),
            ("2026-10-14T23:59:59Z", 10),
            ("2026-10-15T00:00:00Z", 20),
            ("2026-10-20T00:00:00Z", 30),
            ("2026-10-20T00:00:00Z", -5),
            ("2026-11-01T00:00:00Z", 999),
        ]
    ]
    assert expected_totals(rows) == {"events": 4, "units": 55, "amount_microusd": 11500}
    assert expected_totals(rows, repriced=True) == {"events": 4, "units": 55, "amount_microusd": 10875}


def test_workload_reuses_identity_across_accounts_but_not_within_account():
    first = fixture(0)
    assert first == fixture(0)
    originals, late, corrections = first
    all_rows = originals + late + corrections
    assert len(all_rows) == 490
    assert len({(r["source"], r["id"]) for r in all_rows}) == 490
    other = fixture(1)[0]
    assert {(r["source"], r["id"]) for r in originals} == {(r["source"], r["id"]) for r in other}
    by_key = {(r["source"], r["id"]): r for r in originals}
    for correction in corrections:
        original = by_key[correction["source"], correction["correction_of"]]
        assert original["occurred_at"] == correction["occurred_at"]
        assert original["units"] + correction["units"] > 0


@pytest.mark.parametrize("count", [0, 39, 451])
def test_workload_cannot_exceed_sandbox_capacity(count):
    with pytest.raises(ValueError):
        fixture(0, count)


def test_nearest_rank_percentile():
    assert quantile(list(range(1, 101)), 0.95) == 95
