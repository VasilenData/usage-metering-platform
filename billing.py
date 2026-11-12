"""Deterministic, integer-only rating of an immutable usage ledger."""

import hashlib
import json
import re
from datetime import UTC, datetime

ENGINE_VERSION = "1.1.0"


def timestamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A UTC offset is required")
    return result.astimezone(UTC)


def canonical(value):
    def encode(item):
        if isinstance(item, datetime):
            return timestamp(item).isoformat()
        raise TypeError(type(item).__name__)

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def month_bounds(month):
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("month must be YYYY-MM in 2000–2099")
    start = datetime.fromisoformat(month + "-01T00:00:00+00:00")
    end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def rate(events, prices, *, tenant_id, month, known_at=None, engine_version=None):
    """One line per event; corrections use the original event's effective time.

    Input is a database-consistent, known-at snapshot. This function independently
    verifies identity, ownership, correction bounds and complete price coverage.
    """
    version = engine_version or ENGINE_VERSION
    if version not in {"1.0.0", ENGINE_VERSION}:
        raise ValueError("Unsupported rating engine version")
    # Validate ownership before any cutoff can hide a cross-tenant record.
    if any(row["tenant_id"] != tenant_id for row in [*events, *prices]):
        raise ValueError("Mixed tenant snapshot")
    if known_at is not None:
        if version != "1.0.0":
            for rows, field in ((events, "received_at"), (prices, "recorded_at")):
                for row in rows:
                    if field not in row:
                        raise ValueError(f"Known-at rating requires {field} on every source record")
                    try:
                        timestamp(row[field])
                    except (AttributeError, TypeError, ValueError) as exc:
                        raise ValueError(f"Known-at rating requires a valid timezone-aware {field}") from exc
        cutoff = timestamp(known_at)
        events = [e for e in events if "received_at" not in e or timestamp(e["received_at"]) <= cutoff]
        prices = [p for p in prices if "recorded_at" not in p or timestamp(p["recorded_at"]) <= cutoff]
    if version == "1.0.0":
        # Pre-clock statements fingerprinted the original four price columns.
        prices = [{key: value for key, value in p.items() if key != "recorded_at"} for p in prices]
    start, end = month_bounds(month)
    ordered_prices = sorted(prices, key=lambda p: timestamp(p["effective_from"]))
    if len({timestamp(p["effective_from"]) for p in ordered_prices}) != len(ordered_prices):
        raise ValueError("Ambiguous price boundary")
    if any(
        p["tenant_id"] != tenant_id
        or type(p["unit_price_microusd"]) is not int
        or p["unit_price_microusd"] < 0
        for p in prices
    ):
        raise ValueError("Invalid price tenant or integer amount")
    by_key = {}
    for event in events:
        if event["tenant_id"] != tenant_id:
            raise ValueError("Mixed tenant snapshot")
        key = (event["source"], event["id"])
        if key in by_key:
            raise ValueError("Duplicate event identity")
        if type(event["units"]) is not int or event["units"] == 0:
            raise ValueError("Units must be a non-zero integer")
        by_key[key] = event
    balances = {}
    lines = []
    for event in sorted(events, key=lambda e: (e["source"], e["id"])):
        original = event
        reference = event.get("correction_of")
        if reference:
            original = by_key.get((event["source"], reference))
            if original is None or original.get("correction_of"):
                raise ValueError("Correction must reference an original event in this source")
            if timestamp(event["occurred_at"]) != timestamp(original["occurred_at"]):
                raise ValueError("Correction must preserve original occurrence time")
        elif event["units"] < 0:
            raise ValueError("Original usage cannot be negative")
        key = (original["source"], original["id"])
        balances[key] = balances.get(key, 0) + event["units"]
        occurred = timestamp(original["occurred_at"])
        if not start <= occurred < end:
            continue
        applicable = [p for p in ordered_prices if timestamp(p["effective_from"]) <= occurred]
        if not applicable:
            raise ValueError("Unpriced usage; statement publication blocked")
        price = applicable[-1]
        lines.append(
            {
                "source": event["source"],
                "event_id": event["id"],
                "occurred_at": occurred.isoformat(),
                "units": event["units"],
                "correction_of": reference,
                "price_version": price["id"],
                "unit_price_microusd": price["unit_price_microusd"],
                "amount_microusd": event["units"] * price["unit_price_microusd"],
            }
        )
    if any(value < 0 for value in balances.values()):
        raise ValueError("Corrections cannot make original usage negative")
    result = {
        "engine_version": version,
        "tenant_id": tenant_id,
        "month": month,
        "currency": "USD",
        "status": "estimate",
        "lines": lines,
        "total_units": sum(row["units"] for row in lines),
        "amount_microusd": sum(row["amount_microusd"] for row in lines),
        "pricing_version_ids": sorted({row["price_version"] for row in lines}),
    }
    result["source_sha256"] = digest(
        {
            "events": sorted(
                [e for e in events if start <= timestamp(e["occurred_at"]) < end],
                key=lambda e: (e["source"], e["id"]),
            ),
            "prices": [p for p in ordered_prices if p["id"] in result["pricing_version_ids"]],
        }
    )
    result["content_sha256"] = digest(result)
    return result
