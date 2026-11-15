"""Observe real PostgreSQL lock waits, protected HTTP failures and exact retry recovery."""

import argparse
import hashlib
import json
import os
import platform
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from bootstrap_runtime import bootstrap
from runtime_store import RuntimeStore
from verify_http import server, usage
from verify_runtime_restore import fresh_database

ROOT = Path(__file__).resolve().parent


def run(admin_url):
    checks, requests = [], []
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "Synthetic loopback HTTP and deliberately held PostgreSQL locks in an isolated test database",
        "source_commit": os.getenv("GITHUB_SHA", "local-uncommitted"),
        "ci_run_id": os.getenv("GITHUB_RUN_ID"),
        "platform": platform.platform(),
        "checks": checks,
        "requests": requests,
        "limits": [
            "One Uvicorn process and one PostgreSQL node; not production availability or a load benchmark",
            "Lock failures in this exercise precede the writes; a general failed response can follow a commit",
            "Statement and lock budgets are per SQL operation, not a total HTTP deadline",
            "Synthetic AI-assisted verification; not customer traffic or independent human review",
        ],
    }

    def check(name, condition, detail):
        checks.append({"name": name, "passed": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(name)

    def request(client, method, path, **kwargs):
        started = time.perf_counter()
        response = client.request(method, path, **kwargs)
        requests.append(
            {
                "method": method,
                "path": path,
                "status": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return response

    def protected_failure(response):
        return (
            response.status_code == 503
            and response.json()
            == {"detail": "Database operation temporarily unavailable", "outcome": "unknown"}
            and response.headers.get("retry-after") == "2"
            and response.headers.get("cache-control") == "no-store"
            and response.headers.get("x-content-type-options") == "nosniff"
            and "frame-ancestors 'none'" in response.headers.get("content-security-policy", "")
        )

    def observe_waiters(url, blocker_pid, count):
        deadline = time.monotonic() + 4
        with psycopg.connect(url, autocommit=True) as observer:
            while time.monotonic() < deadline:
                rows = observer.execute(
                    "SELECT pid,wait_event_type,wait_event FROM pg_stat_activity "
                    "WHERE datname=current_database() AND usename='meter_api_runtime' "
                    "AND %s=ANY(pg_blocking_pids(pid))",
                    (blocker_pid,),
                ).fetchall()
                if len(rows) >= count:
                    return [{"wait_type": row[1], "wait_event": row[2]} for row in rows]
                # Poll an actual observed condition; do not assume a write boundary by sleeping.
                time.sleep(0.01)
        raise AssertionError("Expected runtime lock waiter was not observed")

    with fresh_database(admin_url, closed=False) as url:
        password = secrets.token_urlsafe(32)
        bootstrap(url, password)
        runtime_url = make_conninfo(url, user="meter_api_runtime", password=password)
        runtime = RuntimeStore(runtime_url)
        with runtime.connection() as connection:
            budgets = {
                key: connection.execute("SHOW " + key).fetchone()[key]
                for key in ("statement_timeout", "lock_timeout")
            }
        check(
            "All runtime connections start with SQL budgets",
            budgets == {"statement_timeout": "5s", "lock_timeout": "2s"},
            budgets,
        )
        with server(ROOT, runtime_url, runtime=True) as client:
            first = request(client, "POST", "/api/sandbox")
            second = request(client, "POST", "/api/sandbox")
            check(
                "Two disposable accounts provision",
                first.status_code == second.status_code == 201,
                "Real restricted API",
            )
            a, b = first.json(), second.json()
            auth_a = {"Authorization": "Bearer " + a["token"]}
            auth_b = {"Authorization": "Bearer " + b["token"]}
            bad = request(client, "POST", "/api/usage", headers=auth_a, json=usage("empty", correction_of=""))
            check(
                "Empty correction reference is a client error",
                bad.status_code == 422,
                "Previously returned HTTP 500",
            )
            with psycopg.connect(url) as connection:
                count = connection.execute(
                    "SELECT count(*) FROM usage_events WHERE tenant_id=%s", (a["tenant_id"],)
                ).fetchone()[0]
            check("Invalid request wrote no ledger event", count == 0, {"rows": count})
            original = request(client, "POST", "/api/usage", headers=auth_a, json=usage("original", 11))
            prior = request(client, "POST", "/api/statements", headers=auth_a, json={"month": "2026-10"})
            check(
                "Initial statement reconciles",
                original.status_code == prior.status_code == 200 and prior.json()["amount_microusd"] == 2750,
                "11 units at 250 microUSD",
            )

            with psycopg.connect(url) as blocker, ThreadPoolExecutor(max_workers=2) as pool:
                blocker.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (a["tenant_id"],))
                waiting_write = pool.submit(
                    request, client, "POST", "/api/usage", headers=auth_a, json=usage("retry-same-id", 7)
                )
                waiting_publish = pool.submit(
                    request, client, "POST", "/api/statements", headers=auth_a, json={"month": "2026-10"}
                )
                observed = observe_waiters(url, blocker.info.backend_pid, 2)
                check(
                    "Write and publication waits independently observed",
                    all(x["wait_type"] == "Lock" for x in observed),
                    observed,
                )
                other = request(client, "POST", "/api/usage", headers=auth_b, json=usage("unblocked-b", 3))
                check(
                    "Another account proceeds during A's lock",
                    other.status_code == 200 and other.json()["accepted"],
                    "No global tenant lock",
                )
                write_response, publish_response = (
                    waiting_write.result(timeout=8),
                    waiting_publish.result(timeout=8),
                )
                check(
                    "Locked write returns protected 503",
                    protected_failure(write_response),
                    "No raw database details; outcome unknown; Retry-After 2",
                )
                check(
                    "Locked publication returns protected 503",
                    protected_failure(publish_response),
                    "No raw database details; outcome unknown; Retry-After 2",
                )
                with psycopg.connect(url) as observer:
                    events = observer.execute(
                        "SELECT id,units FROM usage_events WHERE tenant_id=%s ORDER BY id", (a["tenant_id"],)
                    ).fetchall()
                    documents = observer.execute(
                        "SELECT document FROM statements WHERE tenant_id=%s", (a["tenant_id"],)
                    ).fetchall()
                check(
                    "Timeout leaves prior ledger and statement intact",
                    events == [("original", 11)] and documents == [(prior.json(),)],
                    "Observed while the independent blocker still held the lock",
                )

            retried = request(client, "POST", "/api/usage", headers=auth_a, json=usage("retry-same-id", 7))
            duplicate = request(client, "POST", "/api/usage", headers=auth_a, json=usage("retry-same-id", 7))
            final = request(client, "POST", "/api/statements", headers=auth_a, json={"month": "2026-10"})
            check(
                "Same-identity retry accepts once",
                retried.status_code == duplicate.status_code == 200
                and retried.json()["accepted"]
                and duplicate.json()["duplicate"],
                "No new identity invented after the failed response",
            )
            check(
                "Recovered publication independently reconciles",
                final.status_code == 200
                and final.json()["total_units"] == 18
                and final.json()["amount_microusd"] == 4500
                and final.json()["revision"] == 2,
                "(11+7) × 250 = 4500 microUSD; previous revision retained",
            )

            with psycopg.connect(url) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
                blocker.execute("SELECT pg_advisory_xact_lock(87326002)")
                before = blocker.execute("SELECT count(*) FROM tenants").fetchone()[0]
                provisioning = pool.submit(request, client, "POST", "/api/sandbox")
                observed = observe_waiters(url, blocker.info.backend_pid, 1)
                check(
                    "Provisioning lock wait independently observed",
                    observed[0]["wait_type"] == "Lock",
                    observed,
                )
                refused = provisioning.result(timeout=8)
                check(
                    "Sandbox provisioning is bounded and protected",
                    protected_failure(refused),
                    "The former unbounded runtime path now obeys the connection lock budget",
                )
                after = blocker.execute("SELECT count(*) FROM tenants").fetchone()[0]
                check(
                    "Refused provisioning creates no account",
                    before == after,
                    {"before": before, "after": after},
                )
            recovered = request(client, "POST", "/api/sandbox")
            check(
                "Provisioning recovers after the lock is released",
                recovered.status_code == 201,
                "One deliberate new sandbox request, no automatic retry",
            )
            check(
                "Health remains available after recovery",
                request(client, "GET", "/healthz").status_code == 200,
                "Actual HTTP health route",
            )

    report["source_files"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "app.py",
            "runtime_store.py",
            "store.py",
            "runtime_schema.sql",
            "schema.sql",
            "bootstrap_runtime.py",
            "billing.py",
            "rate_limits.py",
            "verify_http.py",
            "verify_runtime_restore.py",
            "verify_request_recovery.py",
            "requirements.txt",
        )
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise ValueError("Choose a new evidence path; previous reports remain immutable")
    report = run(os.environ["TEST_DATABASE_URL"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "checks": len(report["checks"]),
                "passed": all(row["passed"] for row in report["checks"]),
                "requests": len(report["requests"]),
                "report": str(output),
            }
        )
    )


if __name__ == "__main__":
    main()
