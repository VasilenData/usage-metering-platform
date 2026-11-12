"""Reconcile a deterministic multi-account workload against an independent SQL oracle.

The measured path calls Store, including connection setup and transaction commits;
it is not an HTTP, cloud, sustained-load or maximum-throughput benchmark.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from billing import canonical
from statement_diff import compare_statements
from store import Conflict, Store


def fixture(account, count=450):
    """Fixed seed, reused IDs across accounts/sources, month and price boundaries."""
    if not 40 <= count <= 450:
        raise ValueError("Original event count must be between 40 and 450")
    rows = []
    dates = [
        "2026-09-30T23:59:59Z",
        "2026-10-01T00:00:00Z",
        "2026-10-14T23:59:59Z",
        "2026-10-15T00:00:00Z",
        "2026-10-21T10:00:00Z",
        "2026-10-31T23:59:59Z",
        "2026-11-01T00:00:00Z",
    ]
    for i in range(count):
        rows.append(
            {
                "source": f"api-{i % 3}",
                "id": f"request-{i // 3:04}",
                "occurred_at": dates[i % len(dates)],
                "units": 10 + (i * 17 + account * 23) % 991,
                "correction_of": None,
            }
        )
    late = [
        {
            "source": "api-late",
            "id": f"late-{i:03}",
            "occurred_at": dates[1 + i % 5],
            "units": 5 + i + account,
            "correction_of": None,
        }
        for i in range(20)
    ]
    corrections = [
        dict(row, id=f"adjust-{i:03}", units=-(row["units"] // 2), correction_of=row["id"])
        for i, row in enumerate(rows[:20])
    ]
    random.Random(713 + account).shuffle(rows)
    return rows, late, corrections


def expected_totals(rows, *, repriced=False):
    """Hand-specified fixture arithmetic; no calls to the production rating engine."""
    month_rows = [r for r in rows if r["occurred_at"].startswith("2026-10")]
    total = 0
    for row in month_rows:
        day = int(row["occurred_at"][8:10])
        price = 175 if repriced and day >= 20 else 200 if day >= 15 else 250
        total += row["units"] * price
    return {"events": len(month_rows), "units": sum(r["units"] for r in month_rows), "amount_microusd": total}


def sql_totals(store, tenant_id, cutoff):
    """A separately written relational oracle, under the restricted RLS identity."""
    with store.tenant(tenant_id) as conn:
        row = conn.execute(
            """
            SELECT count(*) AS events, coalesce(sum(e.units),0)::bigint AS units,
                   coalesce(sum(e.units::bigint*p.unit_price_microusd),0)::bigint AS amount_microusd,
                   count(*) FILTER (WHERE p.id IS NULL) AS unpriced
            FROM usage_events e
            LEFT JOIN LATERAL (
                SELECT id,unit_price_microusd FROM prices p
                WHERE p.tenant_id=e.tenant_id AND p.effective_from<=e.occurred_at
                  AND p.recorded_at<=%s
                ORDER BY effective_from DESC LIMIT 1
            ) p ON true
            WHERE e.received_at<=%s AND e.occurred_at>='2026-10-01T00:00:00Z'::timestamptz
              AND e.occurred_at<'2026-11-01T00:00:00Z'::timestamptz
            """,
            (cutoff, cutoff),
        ).fetchone()
    if row.pop("unpriced"):
        raise AssertionError("Independent SQL found unpriced events")
    return row


def quantile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else 0


def run(store, *, accounts=24, originals=450, workers=8):
    if not 1 <= accounts <= 32 or not 1 <= workers <= 16:
        raise ValueError("Use 1–32 accounts and 1–16 clients")
    created = []
    durations = []
    checks = []
    outcomes = []
    case_study = {}

    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})
        if not passed:
            raise AssertionError(name)

    def ingest(item):
        tenant_id, event = item
        start = time.perf_counter()
        response = store.ingest_usage(tenant_id, event)
        return time.perf_counter() - start, response

    def phase(pool, jobs):
        accepted = duplicates = 0
        start = time.perf_counter()
        for elapsed, response in pool.map(ingest, jobs):
            durations.append(elapsed)
            accepted += int(response["accepted"])
            duplicates += int(response["duplicate"])
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "seconds": round(time.perf_counter() - start, 6),
        }

    started = time.perf_counter()
    try:
        for i in range(accounts):
            tenant, _ = store.create_tenant()
            created.append((tenant["id"], fixture(i, originals)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Interleave tenants, preserving intentionally shuffled event arrival.
            jobs = [(tid, data[0][n]) for n in range(originals) for tid, data in created]
            initial = phase(pool, jobs)
            baseline = [store.publish(tid, "2026-10") for tid, _ in created]
            replays = phase(pool, [(tid, row) for tid, data in created for row in data[0][:40]])
            revised = phase(pool, [(tid, row) for tid, data in created for row in data[1] + data[2]])
        check(
            "Every unique event committed exactly once",
            initial["accepted"] == accounts * originals and revised["accepted"] == accounts * 40,
        )
        check(
            "Exact retries added no events",
            replays["duplicates"] == accounts * 40 and replays["accepted"] == 0,
        )
        conflicts = 0
        for tid, data in created:
            try:
                store.ingest_usage(tid, dict(data[0][0], units=data[0][0]["units"] + 1))
            except Conflict:
                conflicts += 1
        check("Changed payloads with reused identity rejected", conflicts == accounts)

        for i, ((tid, data), before) in enumerate(zip(created, baseline, strict=True)):
            current = store.publish(tid, "2026-10")
            # New knowledge with a backdated business-effective date.
            store.provision_price(
                tid, price_id="reviewed-v3", effective_from="2026-10-20T00:00:00Z", unit_price_microusd=175
            )
            repriced = store.publish(tid, "2026-10")
            changes = compare_statements(before, current)
            pricing_changes = compare_statements(current, repriced)
            if i == 0:
                case_study = {
                    "account": "synthetic-01",
                    "usage_restatement": changes,
                    "pricing_restatement": pricing_changes,
                }
            all_rows = data[0] + data[1] + data[2]
            for label, document, rows, revised_price in (
                ("original", before, data[0], False),
                ("late-and-corrected", current, all_rows, False),
                ("repriced", repriced, all_rows, True),
            ):
                expected = expected_totals(rows, repriced=revised_price)
                actual = {
                    "events": len(document["lines"]),
                    "units": document["total_units"],
                    "amount_microusd": document["amount_microusd"],
                }
                cutoff = document.get("known_at", document["created_at"])
                oracle = sql_totals(store, tid, cutoff)
                if expected != actual or expected != oracle:
                    raise AssertionError(
                        {
                            "account": i,
                            "version": label,
                            "fixture": expected,
                            "statement": actual,
                            "sql": oracle,
                        }
                    )
                check(
                    f"Account {i + 1:02} / {label}: fixture = SQL = statement", expected == actual == oracle
                )
            usage, _, _, statements = store.snapshot(tid)
            check(
                f"Account {i + 1:02}: complete ledger and unchanged old revisions",
                len(usage) == originals + 40
                and before in statements
                and current in statements
                and len(statements) == 3
                and store.publish(tid, "2026-10")["id"] == repriced["id"],
            )
            outcomes.append(
                {
                    "account": f"synthetic-{i + 1:02}",
                    "accepted_events": len(usage),
                    "original_microusd": before["amount_microusd"],
                    "corrected_microusd": current["amount_microusd"],
                    "repriced_microusd": repriced["amount_microusd"],
                    "final_units": repriced["total_units"],
                    "reconciliation_difference_microusd": 0,
                    "revisions": 3,
                }
            )
        elapsed = time.perf_counter() - started
        with store.admin() as conn:
            db_version = conn.execute("SELECT version() AS v").fetchone()["v"]
        return {
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope": "Synthetic local Store/PostgreSQL exercise; not HTTP, cloud, sustained load or customer traffic",
            "seed": 713,
            "parameters": {
                "accounts": accounts,
                "originals_per_account": originals,
                "late_per_account": 20,
                "corrections_per_account": 20,
                "replays_per_account": 40,
                "clients": workers,
            },
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "postgresql": db_version,
            },
            "totals": {
                "accepted_events": accounts * (originals + 40),
                "replays_deduplicated": accounts * 40,
                "conflicts_rejected": conflicts,
                "statement_revisions": accounts * 3,
                "reconciliation_difference_microusd": 0,
            },
            "measurement": {
                "whole_exercise_seconds": round(elapsed, 6),
                "ingestion_seconds": round(initial["seconds"] + revised["seconds"], 6),
                "ingestion_p50_seconds": round(quantile(durations, 0.5), 6),
                "ingestion_p95_seconds": round(quantile(durations, 0.95), 6),
                "latency_scope": "Includes accepted and duplicate Store calls, connection setup and tenant locks",
            },
            "checks": checks,
            "case_study": case_study,
            "accounts": outcomes,
            "fixture_sha256": hashlib.sha256(
                canonical([fixture(i, originals) for i in range(accounts)]).encode()
            ).hexdigest(),
        }
    finally:
        # Only IDs allocated by this exercise, in a dedicated disposable database.
        for tid, _ in created:
            with store.admin() as conn:
                conn.execute("DELETE FROM tenants WHERE id=%s", (tid,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, default=24)
    parser.add_argument("--originals", type=int, default=450)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--output", default="local-evidence/workload.json")
    args = parser.parse_args()
    url = os.environ["TEST_DATABASE_URL"]
    if not conninfo_to_dict(url).get("dbname", "").endswith("_test"):
        raise SystemExit("Only a disposable database ending in _test is accepted")
    with psycopg.connect(url) as conn:
        if not conn.execute("SELECT current_database()").fetchone()[0].endswith("_test"):
            raise SystemExit("Connected database is not a disposable _test database")
    result = run(Store(url), accounts=args.accounts, originals=args.originals, workers=args.clients)
    source_names = [
        "verify_workload.py",
        "store.py",
        "billing.py",
        "statement_diff.py",
        "schema.sql",
        "requirements.txt",
    ]
    result["source_files"] = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in source_names
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(canonical({"checks_passed": len(result["checks"]), **result["totals"], "output": str(output)}))


if __name__ == "__main__":
    main()
