"""Executable synthetic fixtures; no customer adoption or commercial claims."""

from datetime import UTC, datetime, timedelta

from analytics import assigned_variant, build_report
from billing import rate


def usage(event_id, units, at="2026-10-03T12:00:00Z", correction_of=None):
    return {
        "id": event_id,
        "source": "demo-api",
        "units": units,
        "occurred_at": at,
        "correction_of": correction_of,
    }


def overview(store, tenant_id):
    events, prices, product, statements = store.snapshot(tenant_id)
    tenant = store.describe(tenant_id)
    now = datetime.now(UTC)
    daily = {}
    for event in events:
        day = event["occurred_at"].date().isoformat()
        daily[day] = daily.get(day, 0) + event["units"]
    months = sorted({e["occurred_at"].strftime("%Y-%m") for e in events})
    total = sum(rate(events, prices, tenant_id=tenant_id, month=month)["amount_microusd"] for month in months)
    return {
        "scope": tenant["scope"],
        "generated_at": now.isoformat(),
        "tenant": {"id": tenant_id, "name": tenant["name"]},
        "metering": {
            "accepted_events": len(events),
            "duplicate_events": tenant["duplicate_events"],
            "total_units": sum(e["units"] for e in events),
            "estimated_amount_microusd": total,
            "currency": "USD",
            "daily_usage": [{"date": day, "units": units} for day, units in sorted(daily.items())],
            "recent_events": [
                dict(e, status="correction" if e["correction_of"] else "accepted")
                for e in sorted(events, key=lambda row: (row["received_at"], row["id"]), reverse=True)[:20]
            ],
            "pricing_versions": prices,
        },
        "statements": statements,
        "analytics": build_report(product, tenant_id=tenant_id, as_of=now),
    }


def seed(store):
    tenant, token = store.create_tenant(demo=True)
    if tenant["seed_complete"]:
        return
    for body in [
        usage("call-001", 12000),
        usage("call-002", 8000, "2026-10-18T10:00:00Z"),
        usage("call-003", 6400, "2026-10-25T10:00:00Z"),
    ]:
        store.ingest_usage(tenant["id"], body)
    store.publish(tenant["id"], "2026-10")
    store.ingest_usage(tenant["id"], usage("late-004", 4000, "2026-10-08T10:00:00Z"))
    store.ingest_usage(tenant["id"], usage("adjust-001", -2000, correction_of="call-001"))
    store.ingest_usage(tenant["id"], usage("call-001", 12000))
    store.publish(tenant["id"], "2026-10")
    start = datetime(2026, 11, 2, 12, tzinfo=UTC)
    for i in range(36):
        actor = f"synthetic-{i:03}"
        variant = assigned_variant(actor)
        at = start + timedelta(hours=i * 2)
        names = ["assignment", "exposure", "signup"]
        if i % 3:
            names.extend(["activation", "api_call"])
        if i % 7 == 0:
            names.append("api_error")
        for j, name in enumerate(names):
            store.ingest_product(
                tenant["id"],
                {
                    "event_id": f"{actor}-{name}",
                    "actor_id": actor,
                    "event_name": name,
                    "occurred_at": at + timedelta(minutes=j),
                    "properties": {"synthetic": True},
                    "experiment_id": "onboarding-v1" if name in ("assignment", "exposure") else None,
                    "variant": variant if name in ("assignment", "exposure") else None,
                },
            )
    with store.admin() as conn:
        conn.execute("UPDATE tenants SET seed_complete=true WHERE id='demo'")


def run_scenario(store, scenario):
    tenant, token = store.create_tenant()
    tenant_id = tenant["id"]
    checks = []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    first = usage("request-1", 100)
    store.ingest_usage(tenant_id, first)
    original = store.publish(tenant_id, "2026-10")
    if scenario == "replay":
        responses = [store.ingest_usage(tenant_id, first) for _ in range(5)]
        result = store.publish(tenant_id, "2026-10")
        check(
            "Idempotent replay", all(x["duplicate"] for x in responses), "Five retries accepted as duplicates"
        )
        check(
            "No second charge",
            result["amount_microusd"] == 25000 and result["id"] == original["id"],
            "100 units × 250 μUSD; same immutable statement",
        )
    elif scenario == "late":
        store.ingest_usage(tenant_id, usage("late-2", 40))
        store.ingest_usage(tenant_id, usage("correction-3", -20, correction_of="request-1"))
        updated = store.publish(tenant_id, "2026-10")
        documents = store.snapshot(tenant_id)[3]
        check(
            "Original preserved",
            any(x == original for x in documents),
            "Revision 1 remains byte-equivalent as a JSON document",
        )
        check(
            "Late usage and correction reconciled",
            updated["total_units"] == 120 and updated["amount_microusd"] == 30000,
            "(100 + 40 − 20) × 250 μUSD = 30,000 μUSD",
        )
        check(
            "New revision published",
            updated["revision"] == 2,
            "No mutation of the previously published statement",
        )
    elif scenario == "pricing":
        store.ingest_usage(tenant_id, usage("after-price-change", 100, "2026-10-15T00:00:00Z"))
        updated = store.publish(tenant_id, "2026-10")
        check(
            "Effective-time pricing",
            updated["amount_microusd"] == 45000,
            "100 × 250 + 100 × 200 = 45,000 μUSD",
        )
        check(
            "Two price versions traceable",
            len(updated["pricing_version_ids"]) == 2,
            "Boundary is inclusive for the new price",
        )
    elif scenario == "isolation":
        with store.tenant(tenant_id) as conn:
            rows = conn.execute("SELECT id FROM usage_events WHERE tenant_id='demo'").fetchall()
            role = conn.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
        check(
            "Cross-account query denied by RLS",
            rows == [],
            "Direct SQL for the seeded demo returns no rows from this sandbox",
        )
        check(
            "Restricted runtime identity",
            not role["rolsuper"] and not role["rolbypassrls"],
            "Runtime is neither superuser nor BYPASSRLS",
        )
    else:
        raise ValueError("Unknown scenario")
    return {
        "scope": "controlled synthetic exercise",
        "checks": checks,
        "overview": overview(store, tenant_id),
        "token": token,
        "tenant_id": tenant_id,
        "expires_at": tenant["expires_at"],
    }
