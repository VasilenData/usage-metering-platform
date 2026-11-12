from copy import deepcopy

import pytest

from analytics import DEFAULT_PROTOCOL, assigned_variant, balanced_srm_pvalue, build_report


def event(identifier, actor, name, time, **extra):
    return {
        "event_id": identifier,
        "tenant_id": "one",
        "actor_id": actor,
        "event_name": name,
        "occurred_at": time,
        "received_at": time,
        **extra,
    }


def report(rows, as_of="2026-11-05T00:00:00Z", **extra):
    return build_report(rows, tenant_id="one", as_of=as_of, **extra)


def protocol(**extra):
    return {
        **deepcopy(DEFAULT_PROTOCOL),
        "ends_at": "2026-11-03T00:00:00Z",
        "minimum_mature_per_variant": 2,
        **extra,
    }


def exposed(actor, variant, *, converted=False, error=False):
    rows = [
        event(
            actor + "-a",
            actor,
            "assignment",
            "2026-11-01T00:00:00Z",
            experiment_id="onboarding-v1",
            variant=variant,
        ),
        event(
            actor + "-e",
            actor,
            "exposure",
            "2026-11-01T00:01:00Z",
            experiment_id="onboarding-v1",
            variant=variant,
        ),
    ]
    if converted:
        rows.append(event(actor + "-c", actor, "activation", "2026-11-01T01:00:00Z"))
    if error:
        rows.append(event(actor + "-x", actor, "api_error", "2026-11-01T02:00:00Z"))
    return rows


def test_ordered_funnel_excludes_out_of_order_steps():
    rows = [
        event("1", "a", "signup", "2026-11-01T00:00:00Z"),
        event("2", "a", "activation", "2026-11-01T01:00:00Z"),
        event("3", "a", "api_call", "2026-11-01T02:00:00Z"),
        event("4", "b", "activation", "2026-11-01T00:00:00Z"),
        event("5", "b", "signup", "2026-11-01T01:00:00Z"),
        event("6", "b", "api_call", "2026-11-01T02:00:00Z"),
        event("7", "c", "signup", "2026-11-01T00:00:00Z"),
        event("8", "c", "api_call", "2026-11-01T01:00:00Z"),
        event("9", "c", "activation", "2026-11-01T02:00:00Z"),
    ]
    result = report(rows)
    assert [r["actors"] for r in result["funnel"]] == [3, 2, 1]


def test_session_boundary_and_metadata_do_not_extend_session():
    rows = [
        event("1", "a", "signup", "2026-11-01T00:00:00Z"),
        event("2", "a", "activation", "2026-11-01T00:30:00Z"),
        event("3", "a", "assignment", "2026-11-01T00:50:00Z", experiment_id="other", variant="control"),
        event("4", "a", "api_call", "2026-11-01T01:00:01Z"),
        event("5", "b", "api_call", "2026-11-01T01:00:01Z"),
    ]
    assert report(rows)["sessions"] == 3


def test_retention_uses_complete_next_utc_calendar_day():
    rows = [
        event("1", "a", "signup", "2026-11-01T23:30:00Z"),
        event("2", "a", "api_call", "2026-11-02T00:00:00Z"),
        event("3", "b", "signup", "2026-11-01T01:00:00Z"),
        event("4", "b", "api_call", "2026-11-03T00:00:00Z"),
        event("5", "c", "signup", "2026-11-02T10:00:00Z"),
        event("6", "c", "api_call", "2026-11-02T11:00:00Z"),
    ]
    before = report(rows, "2026-11-02T23:59:59Z")["day1_retention"]
    assert before == {"eligible_actors": 0, "retained_actors": 0, "rate": None}
    after = report(rows, "2026-11-03T00:00:00Z")["day1_retention"]
    assert after == {"eligible_actors": 2, "retained_actors": 1, "rate": 0.5}


def test_timezone_offsets_normalized_before_retention_cohort():
    rows = [
        event("1", "a", "signup", "2026-11-02T00:30:00+02:00"),
        event("2", "a", "api_call", "2026-11-02T00:30:00Z"),
    ]
    assert report(rows, "2026-11-03T00:00:00Z")["day1_retention"]["rate"] == 1.0


def test_late_received_event_cannot_change_earlier_known_at_snapshot():
    row = event("1", "a", "signup", "2026-11-01T00:00:00Z", received_at="2026-11-06T00:00:00Z")
    assert report([row])["events"] == 0
    assert report([row], "2026-11-06T00:00:00Z")["events"] == 1


def test_replays_deduplicate_without_changing_source_or_metrics():
    row = event("1", "a", "signup", "2026-11-01T00:00:00Z")
    replay = {**row, "received_at": "2026-11-02T00:00:00Z"}
    original = report([row])
    repeated = report([replay, row])
    assert repeated["events"] == 1
    assert repeated["duplicate_records_excluded"] == 1
    assert original["source_sha256"] == repeated["source_sha256"]


def test_duplicate_id_with_changed_contents_fails():
    row = event("1", "a", "signup", "2026-11-01T00:00:00Z")
    with pytest.raises(ValueError, match="different contents"):
        report([row, {**row, "actor_id": "b"}])


def test_mixed_tenant_fails_even_if_event_outside_snapshot():
    row = event("1", "a", "signup", "2099-11-01T00:00:00Z", tenant_id="two")
    with pytest.raises(ValueError, match="mixed-tenant"):
        report([row])


def test_naive_clocks_rejected():
    with pytest.raises(ValueError, match="timezone"):
        report([event("1", "a", "signup", "2026-11-01T00:00:00")])


def test_pre_exposure_and_end_boundary_conversions_excluded():
    rows = exposed("a", "control")
    rows += [
        event("a-before", "a", "activation", "2026-11-01T00:00:30Z"),
        event("a-end", "a", "activation", "2026-11-02T00:01:00Z"),
    ]
    result = report(rows)["experiment"]["arms"]["control"]
    assert result["mature_exposed_actors"] == 1
    assert result["converted_actors"] == 0


def test_immature_exposures_do_not_enter_denominator():
    result = report(exposed("a", "control", converted=True), "2026-11-01T23:59:59Z")["experiment"]
    assert result["arms"]["control"]["mature_exposed_actors"] == 0
    assert result["arms"]["control"]["conversion_rate"] is None
    assert result["quality"]["immature_exposure"] == 1


@pytest.mark.parametrize("failure", ["missing_assignment", "crossover", "before_assignment"])
def test_invalid_exposure_blocks_decision(failure):
    rows = exposed("a", "control", converted=True)
    if failure == "missing_assignment":
        rows = rows[1:]
    elif failure == "crossover":
        rows[1]["variant"] = "treatment"
    else:
        rows[0]["occurred_at"] = "2026-11-01T00:02:00Z"
    result = report(rows, protocol=protocol(), synthetic=False)["experiment"]
    assert result["arms"]["control"]["mature_exposed_actors"] == 0
    assert "Assignment/exposure integrity failed." in result["reasons"]


def test_exact_srm_known_small_examples():
    assert balanced_srm_pvalue(0, 0) is None
    assert balanced_srm_pvalue(2, 2) == pytest.approx(1)
    assert balanced_srm_pvalue(0, 4) == pytest.approx(0.125)
    assert balanced_srm_pvalue(1, 3) == pytest.approx(0.625)


def test_large_synthetic_effect_never_becomes_launch_evidence():
    rows = []
    for i in range(30):
        rows += exposed("c" + str(i), "control")
        rows += exposed("t" + str(i), "treatment", converted=True)
    result = report(rows, protocol=protocol())["experiment"]
    assert result["absolute_lift"] == 1
    assert result["absolute_lift_interval_95"][0] > 0
    assert result["decision"] == "do_not_launch"
    assert any("Synthetic" in r for r in result["reasons"])


def test_real_instrumentation_still_requires_closed_horizon_and_guardrail():
    rows = []
    for i in range(30):
        rows += exposed("c" + str(i), "control")
        rows += exposed("t" + str(i), "treatment", converted=True, error=(i < 5))
    result = report(rows, protocol=protocol(ends_at="2026-12-01T00:00:00Z"), synthetic=False)["experiment"]
    assert result["decision"] == "do_not_launch"
    assert any("horizon" in r for r in result["reasons"])
    assert "Treatment error-rate guardrail failed." in result["reasons"]


def test_complete_non_synthetic_protocol_returns_review_not_automatic_launch():
    rows = []
    for i in range(30):
        rows += exposed("c" + str(i), "control")
        rows += exposed("t" + str(i), "treatment", converted=True)
    result = report(rows, protocol=protocol(), synthetic=False)["experiment"]
    assert result["decision"] == "eligible_for_review"


def test_source_and_report_are_input_order_invariant():
    rows = exposed("c", "control", converted=True) + exposed("t", "treatment")
    assert report(rows) == report(list(reversed(rows)))


def test_assignment_is_stable_and_versioned():
    assert assigned_variant("actor-1") in {"control", "treatment"}
    assert assigned_variant("actor-1") == assigned_variant("actor-1")
    assert {assigned_variant(f"actor-{i}") for i in range(30)} == {"control", "treatment"}


def test_empty_denominators_are_unknown_not_zero_percent():
    result = report([])
    assert result["sessions"] == 0
    assert result["day1_retention"]["rate"] is None
    assert result["experiment"]["absolute_lift"] is None
    assert result["experiment"]["decision"] == "do_not_launch"
