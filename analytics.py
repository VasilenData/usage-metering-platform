"""Tenant-scoped product metrics with explicit clocks and fixed-horizon gates.

This module is pure: it does not ingest, authenticate, persist or expose events.
The API must scope the query before calling it; mixed-tenant input fails closed.
"""

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

EVENT_NAMES = frozenset({"signup", "activation", "api_call", "api_error", "assignment", "exposure"})
BEHAVIOUR_EVENTS = EVENT_NAMES - {"assignment", "exposure"}
DEFAULT_PROTOCOL = {
    "experiment_id": "onboarding-v1",
    "version": "1",
    "starts_at": "2026-11-01T00:00:00Z",
    "ends_at": "2026-12-01T00:00:00Z",
    "conversion_window_hours": 24,
    "minimum_mature_per_variant": 100,
    "minimum_absolute_lift": 0.05,
    "srm_alpha": 0.01,
    "maximum_treatment_error_rate": 0.05,
    "primary_metric": "activation within 24 hours of first eligible exposure",
    "guardrail": "fraction of mature exposed treatment actors with an api_error within 24 hours",
    "allocation": {"control": 0.5, "treatment": 0.5},
    "analysis": "fixed horizon; no early launch decision",
}


def utc(value):
    """Require a timezone rather than silently interpreting an ambiguous clock."""
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def assigned_variant(actor_id, experiment_id="onboarding-v1", version="1"):
    """Stable 50:50 allocation, independent of Python's randomized hash seed."""
    digest = hashlib.sha256(f"{experiment_id}:{version}:{actor_id}".encode()).digest()
    return "control" if int.from_bytes(digest[:8], "big") % 10000 < 5000 else "treatment"


def wilson_interval(successes, total):
    """Approximate 95% binomial score interval; no rate for an empty denominator."""
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    width = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - width), min(1.0, center + width)]


def balanced_srm_pvalue(control, treatment):
    """Exact two-sided binomial allocation test for an explicitly 50:50 protocol."""
    n = control + treatment
    if n == 0:
        return None
    k = min(control, treatment)
    # Summing the smaller tail in log space prevents factorial overflow.
    logs = [
        math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) - n * math.log(2)
        for j in range(k + 1)
    ]
    largest = max(logs)
    tail = math.exp(largest) * math.fsum(math.exp(x - largest) for x in logs)
    return min(1.0, 2 * tail)


def _canonical_events(events, tenant_id, as_of):
    unique = {}
    conflicts = 0
    for raw in events:
        if raw.get("tenant_id") != tenant_id:
            raise ValueError("mixed-tenant analytics input rejected")
        event = dict(raw)
        for field in ("event_id", "actor_id"):
            if not isinstance(event.get(field), str) or not event[field]:
                raise ValueError(f"{field} is required")
        if event.get("event_name") not in EVENT_NAMES:
            raise ValueError("unknown event_name")
        occurred, received = utc(event["occurred_at"]), utc(event["received_at"])
        if occurred > as_of or received > as_of:
            continue
        event["occurred_at"] = occurred
        event["received_at"] = received
        identity = event["event_id"]
        previous = unique.get(identity)
        if previous is not None:
            # A replay can have a later received_at, but its semantic contents cannot change.
            old = {k: v for k, v in previous.items() if k != "received_at"}
            new = {k: v for k, v in event.items() if k != "received_at"}
            if old != new:
                raise ValueError("event_id was reused with different contents")
            conflicts += 1
            if received < previous["received_at"]:
                unique[identity] = event
        else:
            unique[identity] = event
    return sorted(unique.values(), key=lambda e: (e["occurred_at"], e["event_id"])), conflicts


def _experiment(events, as_of, protocol, synthetic):
    p = dict(protocol)
    if p["allocation"] != {"control": 0.5, "treatment": 0.5}:
        raise ValueError("this implementation supports a declared 50:50 allocation only")
    start, end = utc(p["starts_at"]), utc(p["ends_at"])
    window = timedelta(hours=p["conversion_window_hours"])
    if end <= start or window <= timedelta(0) or p["minimum_mature_per_variant"] < 1:
        raise ValueError("invalid experiment window or sample threshold")
    assignments, exposures, behaviours = defaultdict(list), defaultdict(list), defaultdict(list)
    for event in events:
        if event["event_name"] in BEHAVIOUR_EVENTS:
            behaviours[event["actor_id"]].append(event)
        if event.get("experiment_id") != p["experiment_id"]:
            continue
        if not start <= event["occurred_at"] < end:
            continue
        target = assignments if event["event_name"] == "assignment" else exposures
        if event["event_name"] in {"assignment", "exposure"}:
            target[event["actor_id"]].append(event)
    quality = Counter()
    assigned = Counter()
    eligible = []
    for actor in sorted(set(assignments) | set(exposures)):
        records = assignments[actor]
        variants = {e.get("variant") for e in records}
        if not records:
            quality["exposure_without_assignment"] += 1
            continue
        if len(variants) != 1 or not variants <= {"control", "treatment"}:
            quality["conflicting_assignment"] += 1
            continue
        variant = next(iter(variants))
        first_assignment = min(e["occurred_at"] for e in records)
        assigned[variant] += 1
        actor_exposures = exposures[actor]
        if not actor_exposures:
            continue
        if any(e.get("variant") != variant or e["occurred_at"] < first_assignment for e in actor_exposures):
            quality["invalid_exposure"] += 1
            continue
        exposed_at = min(e["occurred_at"] for e in actor_exposures)
        if exposed_at + window > as_of:
            quality["immature_exposure"] += 1
            continue
        actor_window = [e for e in behaviours[actor] if exposed_at <= e["occurred_at"] < exposed_at + window]
        eligible.append(
            (
                variant,
                any(e["event_name"] == "activation" for e in actor_window),
                any(e["event_name"] == "api_error" for e in actor_window),
            )
        )
    arms = {}
    for variant in ("control", "treatment"):
        rows = [e for e in eligible if e[0] == variant]
        n, converted, errors = len(rows), sum(e[1] for e in rows), sum(e[2] for e in rows)
        arms[variant] = {
            "assigned_actors": assigned[variant],
            "mature_exposed_actors": n,
            "converted_actors": converted,
            "conversion_rate": converted / n if n else None,
            "conversion_interval_95": wilson_interval(converted, n),
            "error_actors": errors,
            "error_rate": errors / n if n else None,
        }
    srm = balanced_srm_pvalue(assigned["control"], assigned["treatment"])
    control, treatment = arms["control"], arms["treatment"]
    difference, interval = None, None
    if control["conversion_rate"] is not None and treatment["conversion_rate"] is not None:
        a, b = control["conversion_rate"], treatment["conversion_rate"]
        la, ua = control["conversion_interval_95"]
        lb, ub = treatment["conversion_interval_95"]
        difference = b - a
        # Newcombe's unpooled score interval for difference between independent proportions.
        interval = [
            difference - math.sqrt((b - lb) ** 2 + (ua - a) ** 2),
            difference + math.sqrt((ub - b) ** 2 + (a - la) ** 2),
        ]
    reasons = []
    invalid = sum(
        quality[k] for k in ("exposure_without_assignment", "conflicting_assignment", "invalid_exposure")
    )
    if synthetic:
        reasons.append("Synthetic exercise: no causal or commercial launch claim.")
    if invalid:
        reasons.append("Assignment/exposure integrity failed.")
    if srm is not None and srm < p["srm_alpha"]:
        reasons.append("Assignment sample-ratio mismatch detected.")
    if as_of < end + window:
        reasons.append("The fixed experiment horizon and outcome window have not closed.")
    if any(arms[v]["mature_exposed_actors"] < p["minimum_mature_per_variant"] for v in arms):
        reasons.append("Insufficient mature exposed actors under the declared protocol.")
    if treatment["error_rate"] is not None and treatment["error_rate"] > p["maximum_treatment_error_rate"]:
        reasons.append("Treatment error-rate guardrail failed.")
    if reasons:
        decision = "do_not_launch"
    elif difference >= p["minimum_absolute_lift"] and interval[0] > 0:
        decision = "eligible_for_review"
        reasons.append(
            "Fixed-horizon primary and guardrail gates passed; requires review of implementation and assignment validity."
        )
    else:
        decision = "do_not_launch"
        reasons.append(
            "The estimated improvement does not satisfy both the effect-size and uncertainty gates."
        )
    return {
        "protocol": p,
        "protocol_sha256": hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest(),
        "arms": arms,
        "assignment_srm_pvalue": srm,
        "quality": dict(quality),
        "absolute_lift": difference,
        "absolute_lift_interval_95": interval,
        "decision": decision,
        "reasons": reasons,
        "interval_method": "Wilson binomial / Newcombe unpooled difference, approximate 95%",
        "limits": [
            "Random assignment and complete instrumentation are prerequisites for causal interpretation.",
            "No CUPED: no pre-experiment covariate is collected.",
            "The minimum sample threshold is a protocol floor, not a substitute for a power calculation.",
        ],
    }


def build_report(events, *, tenant_id, as_of, protocol=None, synthetic=True):
    """Compute a reproducible snapshot from events known at `as_of` in one tenant."""
    cutoff = utc(as_of)
    rows, duplicates = _canonical_events(events, tenant_id, cutoff)
    by_actor = defaultdict(list)
    for event in rows:
        if event["event_name"] in BEHAVIOUR_EVENTS:
            by_actor[event["actor_id"]].append(event)
    sessions = 0
    funnel = Counter()
    eligible_retention, retained = 0, 0
    for actor_rows in by_actor.values():
        previous = None
        for event in actor_rows:
            if previous is None or event["occurred_at"] - previous > timedelta(minutes=30):
                sessions += 1
            previous = event["occurred_at"]
        signups = [e["occurred_at"] for e in actor_rows if e["event_name"] == "signup"]
        if not signups:
            continue
        signup = min(signups)
        funnel["signup"] += 1
        activations = [
            e["occurred_at"]
            for e in actor_rows
            if e["event_name"] == "activation" and e["occurred_at"] >= signup
        ]
        if activations:
            activated = min(activations)
            funnel["activation"] += 1
            if any(e["event_name"] == "api_call" and e["occurred_at"] >= activated for e in actor_rows):
                funnel["api_call"] += 1
        next_day = datetime.combine(signup.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
        if cutoff >= next_day + timedelta(days=1):
            eligible_retention += 1
            if any(
                e["event_name"] in {"activation", "api_call"}
                and next_day <= e["occurred_at"] < next_day + timedelta(days=1)
                for e in actor_rows
            ):
                retained += 1
    source = [
        {**e, "occurred_at": e["occurred_at"].isoformat(), "received_at": e["received_at"].isoformat()}
        for e in rows
    ]
    return {
        "tenant_id": tenant_id,
        "as_of": cutoff.isoformat(),
        "scope": "synthetic demo" if synthetic else "instrumented events",
        "source_sha256": hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest(),
        "events": len(rows),
        "duplicate_records_excluded": duplicates,
        "actors": len(by_actor),
        "sessions": sessions,
        "event_counts": dict(Counter(e["event_name"] for e in rows)),
        "funnel": [{"event": name, "actors": funnel[name]} for name in ("signup", "activation", "api_call")],
        "day1_retention": {
            "eligible_actors": eligible_retention,
            "retained_actors": retained,
            "rate": retained / eligible_retention if eligible_retention else None,
        },
        "experiment": _experiment(rows, cutoff, protocol or DEFAULT_PROTOCOL, synthetic),
        "definitions": {
            "session": "New session after more than 30 minutes without a behavioural event; assignment/exposure excluded.",
            "funnel": "Distinct actors with ordered signup → activation → api_call across available history; ties count as ordered.",
            "day1_retention": "Activation or api_call on the next UTC calendar day after first signup; only fully elapsed next days enter the denominator.",
            "as_of": "Both occurred_at and received_at must be at or before the cutoff; this is a known-at snapshot.",
        },
    }
