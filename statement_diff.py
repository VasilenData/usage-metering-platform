"""Explain an immutable statement revision as a conserved, event-level bridge."""

from billing import digest, timestamp

METADATA = frozenset({"id", "revision", "created_at", "known_at", "content_sha256"})


def _validate(document):
    core = {key: value for key, value in document.items() if key not in METADATA}
    if digest(core) != document["content_sha256"]:
        raise ValueError("Statement content fingerprint mismatch")
    lines = {}
    for line in document["lines"]:
        key = (line["source"], line["event_id"])
        if key in lines:
            raise ValueError("Duplicate statement line")
        for field in ("units", "unit_price_microusd", "amount_microusd"):
            if type(line[field]) is not int:
                raise ValueError("Statement money and units must be integers")
        if line["amount_microusd"] != line["units"] * line["unit_price_microusd"]:
            raise ValueError("Statement line amount does not reconcile")
        lines[key] = line
    if sum(line["units"] for line in lines.values()) != document["total_units"]:
        raise ValueError("Statement units do not reconcile")
    if sum(line["amount_microusd"] for line in lines.values()) != document["amount_microusd"]:
        raise ValueError("Statement amount does not reconcile")
    return lines


def compare_statements(before, after):
    """Attribute a forward revision to new usage, corrections or price changes.

    A correction introduced in the new revision is valued at that revision's
    price. Repricing existing lines is attributed separately; the sum therefore
    equals the actual statement change without an order-dependent residual.
    """
    for field in ("tenant_id", "month", "currency"):
        if before[field] != after[field]:
            raise ValueError(f"Cannot compare different {field}")
    if after["revision"] <= before["revision"]:
        raise ValueError("Comparison requires a forward statement revision")
    if timestamp(after.get("known_at", after["created_at"])) < timestamp(
        before.get("known_at", before["created_at"])
    ):
        raise ValueError("Statement knowledge cutoff moved backwards")
    old, new = _validate(before), _validate(after)
    if old.keys() - new.keys():
        raise ValueError("Append-only statement lost a prior event")
    changes = []
    summary = {
        reason: {"lines": 0, "delta_units": 0, "delta_amount_microusd": 0}
        for reason in ("late_usage", "usage_correction", "price_change")
    }
    for key, line in sorted(new.items()):
        previous = old.get(key)
        if previous is None:
            reason = "usage_correction" if line.get("correction_of") else "late_usage"
            delta_units = line["units"]
            delta_amount = line["amount_microusd"]
        else:
            for field in ("units", "occurred_at", "correction_of"):
                if previous.get(field) != line.get(field):
                    raise ValueError("Immutable source event changed between revisions")
            if previous == line:
                continue
            reason = "price_change"
            delta_units = 0
            delta_amount = line["amount_microusd"] - previous["amount_microusd"]
        changes.append(
            {
                "source": key[0],
                "event_id": key[1],
                "reason": reason,
                "delta_units": delta_units,
                "delta_amount_microusd": delta_amount,
                "before_amount_microusd": previous["amount_microusd"] if previous else 0,
                "after_amount_microusd": line["amount_microusd"],
                "before_price_version": previous["price_version"] if previous else None,
                "after_price_version": line["price_version"],
            }
        )
        summary[reason]["lines"] += 1
        summary[reason]["delta_units"] += delta_units
        summary[reason]["delta_amount_microusd"] += delta_amount
    delta_units = after["total_units"] - before["total_units"]
    delta_amount = after["amount_microusd"] - before["amount_microusd"]
    if (
        sum(row["delta_units"] for row in changes) != delta_units
        or sum(row["delta_amount_microusd"] for row in changes) != delta_amount
    ):
        raise ValueError("Statement bridge does not conserve the published change")
    return {
        "tenant_id": before["tenant_id"],
        "month": before["month"],
        "currency": before["currency"],
        "before_id": before["id"],
        "after_id": after["id"],
        "before_revision": before["revision"],
        "after_revision": after["revision"],
        "before_amount_microusd": before["amount_microusd"],
        "after_amount_microusd": after["amount_microusd"],
        "delta_units": delta_units,
        "delta_amount_microusd": delta_amount,
        "summary": summary,
        "changes": changes,
        "attribution": "New events at the new revision price; existing-event repricing separately.",
    }
