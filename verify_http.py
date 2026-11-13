"""Bounded real-Uvicorn/PostgreSQL exercise; never a capacity or penetration test."""

import argparse
import hashlib
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict

from store import Store
from verify_workload import quantile, sql_totals

BASELINE_COMMIT = "94a4cb1885297442ef59bdcf9fcecc605ff2a97b"


def verified_baseline(directory):
    """Require the advertised immutable checkout, not merely a plausible directory."""
    directory = Path(directory).resolve()

    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(directory), *arguments])

    if git("rev-parse", "HEAD").decode().strip() != BASELINE_COMMIT:
        raise ValueError("Baseline checkout does not match the pinned commit")
    names = git("ls-tree", "-r", "--name-only", BASELINE_COMMIT).decode().splitlines()
    hashes = {}
    for name in names:
        path = directory / name
        expected = git("show", BASELINE_COMMIT + ":" + name)
        if not path.is_file() or path.read_bytes() != expected:
            raise ValueError("Baseline tracked files differ from the pinned bytes")
        hashes[name] = hashlib.sha256(expected).hexdigest()
    known_python = {name for name in names if name.endswith(".py")}
    actual_python = {path.relative_to(directory).as_posix() for path in directory.rglob("*.py")}
    if actual_python != known_python:
        raise ValueError("Baseline contains untracked Python sources that could shadow imports")
    return directory, hashes


def usage(event_id, units=10, **extra):
    return {
        "id": event_id,
        "source": "http-exercise",
        "occurred_at": "2026-10-05T12:00:00Z",
        "units": units,
        **extra,
    }


def product(properties):
    return (
        '{"event_id":"unsupported","actor_id":"actor","event_name":"signup",'
        '"occurred_at":"2026-10-05T12:00:00Z","properties":' + properties + "}"
    ).encode()


@contextmanager
def server(directory, url):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # Logs are private temporary files: neither bearer tokens nor connection URLs
    # are printed or placed in the evidence artifact. Access logging is disabled.
    with tempfile.TemporaryFile() as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=directory,
            env={**os.environ, "DATABASE_URL": url},
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                for _ in range(100):
                    if child.poll() is not None:
                        raise RuntimeError("Uvicorn exited during startup; private server log withheld")
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Uvicorn readiness timed out")
                yield client
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="local-evidence/http-ci.json")
    parser.add_argument(
        "--baseline-dir", help="Optional immutable pre-fix checkout for actual HTTP reproduction"
    )
    args = parser.parse_args()
    url = os.environ["TEST_DATABASE_URL"]
    if not conninfo_to_dict(url).get("dbname", "").endswith("_test"):
        raise SystemExit("A dedicated PostgreSQL database ending in _test is required")
    with psycopg.connect(url) as conn:
        if not conn.execute("SELECT current_database()").fetchone()[0].endswith("_test"):
            raise SystemExit("Refusing a non-test database")
    store = Store(url)
    created = []
    records = []
    checks = []
    lock = threading.Lock()
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "Synthetic HTTP requests to one real Uvicorn process and PostgreSQL 17 over loopback; no public backend",
        "host": platform.platform(),
        "execution_environment": "GitHub-hosted Linux" if os.getenv("GITHUB_ACTIONS") == "true" else "local",
        "source_commit": os.getenv("GITHUB_SHA", "uncommitted-local"),
        "ci_run_id": os.getenv("GITHUB_RUN_ID"),
        "ci_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
        "client_workers": 8,
        "checks": checks,
        "requests": records,
        "limits": [
            "Not a penetration test, maximum-throughput benchmark, production SLO or evidence of real customers",
            "One process and one PostgreSQL node; no TLS, proxy, internet path or distributed limiter exercised",
            "AI-assisted implementation and review; not an independent human review or assessment of owner proficiency",
        ],
    }

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise AssertionError(name)

    def request(client, scenario, method, path, token=None, protect=True, **kwargs):
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = "Bearer " + token
        started = time.perf_counter()
        response = client.request(method, path, headers=headers, **kwargs)
        row = {
            "scenario": scenario,
            "status": response.status_code,
            "seconds": round(time.perf_counter() - started, 6),
        }
        with lock:
            records.append(row)
        if protect:
            assert response.headers.get("cache-control") == "no-store", scenario
            assert response.headers.get("x-content-type-options") == "nosniff", scenario
            assert "frame-ancestors 'none'" in response.headers.get("content-security-policy", ""), scenario
        return response

    def sandbox(client, scenario="sandbox", protect=True):
        response = request(client, scenario, "POST", "/api/sandbox", protect=protect)
        assert response.status_code == 201
        body = response.json()
        created.append(body["tenant_id"])
        return body["tenant_id"], body["token"]

    def batch(callback, count):
        barrier = threading.Barrier(min(8, count))

        def run(index):
            # A barrier aligns the first wave; later waves remain bounded to 8 clients.
            if index < 8:
                barrier.wait(timeout=15)
            return callback(index)

        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(run, range(count)))

    try:
        if args.baseline_dir:
            baseline_directory, baseline_hashes = verified_baseline(args.baseline_dir)
            report["baseline_commit"] = BASELINE_COMMIT
            report["baseline_source_files"] = baseline_hashes
            with server(baseline_directory, url) as client:
                _, token = sandbox(client, "baseline-sandbox", protect=False)
                response = request(
                    client,
                    "baseline-oversize",
                    "POST",
                    "/api/events",
                    token,
                    protect=False,
                    content=b"x" * 8193,
                )
                check(
                    "Pre-fix oversize response lacks security/cache headers",
                    response.status_code == 413 and "cache-control" not in response.headers,
                    "Reproduced over HTTP against the pinned pre-fix commit",
                )
                failures = []
                for name, properties in [
                    ("nan", '{"x":NaN}'),
                    ("overflow", '{"x":1e400}'),
                    ("nul", '{"x":"\\u0000"}'),
                ]:
                    failures.append(
                        request(
                            client,
                            "baseline-" + name,
                            "POST",
                            "/api/events",
                            token,
                            protect=False,
                            content=product(properties),
                            # The old server re-raises after its 500; do not race
                            # that connection's close when probing the next case.
                            headers={"Content-Type": "application/json", "Connection": "close"},
                        ).status_code
                    )
                check(
                    "Pre-fix unsupported product values produce HTTP 500",
                    failures == [500, 500, 500],
                    "NaN, overflow to infinity, and escaped NUL reach PostgreSQL jsonb; baseline only",
                )

        with server(Path.cwd(), url) as client:
            tenant_a, token_a = sandbox(client)
            tenant_b, token_b = sandbox(client)
            tenant_c, token_c = sandbox(client)

            rows = batch(
                lambda _: request(
                    client, "exact-replay", "POST", "/api/usage", token_a, json=usage("replay", 100)
                ),
                24,
            )
            check(
                "Concurrent exact replay commits once",
                all(r.status_code == 200 for r in rows) and sum(r.json()["accepted"] for r in rows) == 1,
                "24 HTTP requests, 8 clients, one accepted row and 23 duplicate acknowledgements",
            )

            rows = batch(
                lambda i: request(
                    client,
                    "conflicting-identity",
                    "POST",
                    "/api/usage",
                    token_a,
                    json=usage("conflict", 10 + i % 2),
                ),
                16,
            )
            accepted = [r.json() for r in rows if r.status_code == 200]
            check(
                "Competing payloads cannot rewrite identity",
                Counter(r.status_code for r in rows) == {200: 8, 409: 8}
                and sum(r["accepted"] for r in accepted) == 1,
                "One of two payloads wins; its seven replays succeed and all eight opposing payloads conflict",
            )

            response = request(
                client, "tenant-isolation", "POST", "/api/usage", token_b, json=usage("replay", 7)
            )
            assert response.status_code == 200 and response.json()["accepted"]
            assert (
                request(
                    client, "tenant-isolation", "POST", "/api/usage", token_a, json=usage("only-a", 20)
                ).status_code
                == 200
            )
            response = request(
                client,
                "cross-tenant-correction",
                "POST",
                "/api/usage",
                token_b,
                json=usage("foreign", -1, correction_of="only-a"),
            )
            check(
                "Cross-account correction rejected",
                response.status_code == 422,
                "An original present only in account A is invisible from account B",
            )
            response = request(
                client,
                "tenant-selector-body",
                "POST",
                "/api/usage",
                token_b,
                json={**usage("injected"), "tenant_id": tenant_a},
            )
            check(
                "Body cannot override authenticated account",
                response.status_code == 422,
                "Strict model rejects a tenant selector",
            )

            assert (
                request(
                    client, "correction-base", "POST", "/api/usage", token_c, json=usage("base", 100)
                ).status_code
                == 200
            )
            rows = batch(
                lambda i: request(
                    client,
                    "competing-corrections",
                    "POST",
                    "/api/usage",
                    token_c,
                    json=usage(f"correction-{i}", -20, correction_of="base"),
                ),
                8,
            )
            check(
                "Concurrent corrections preserve nonnegative balance",
                Counter(r.status_code for r in rows) == {200: 5, 422: 3},
                "Only five -20 corrections can consume the same original 100 units",
            )

            rows = batch(
                lambda _: request(
                    client,
                    "assignment-replay",
                    "POST",
                    "/api/assignment",
                    token_a,
                    json={"actor_id": "same-actor"},
                ),
                8,
            )
            check(
                "Concurrent first assignment is stable",
                all(r.status_code == 200 and r.json() == rows[0].json() for r in rows),
                "Eight real HTTP requests receive one immutable assignment",
            )

            rows = batch(
                lambda _: request(
                    client,
                    "publication-replay",
                    "POST",
                    "/api/statements",
                    token_a,
                    json={"month": "2026-10"},
                ),
                8,
            )
            check(
                "Concurrent unchanged publication reuses revision",
                all(r.status_code == 200 for r in rows) and len({r.json()["id"] for r in rows}) == 1,
                "Eight callers receive the same statement identity",
            )
            original = rows[0].json()

            def mixed(index):
                if index % 2:
                    return request(
                        client,
                        "publication-race",
                        "POST",
                        "/api/statements",
                        token_a,
                        json={"month": "2026-10"},
                    )
                return request(
                    client,
                    "publication-race",
                    "POST",
                    "/api/usage",
                    token_a,
                    json=usage(f"racing-{index}", index + 1),
                )

            rows = batch(mixed, 16)
            assert all(r.status_code == 200 for r in rows)
            final = request(
                client, "publication-final", "POST", "/api/statements", token_a, json={"month": "2026-10"}
            ).json()
            documents = request(
                client, "tenant-selector-query", "GET", "/api/statements?tenant_id=" + tenant_b, token_a
            ).json()
            check(
                "Authenticated statement reads remain isolated",
                all(d["tenant_id"] == tenant_a for d in documents) and original in documents,
                "A query parameter cannot change token scope; original revision remains intact",
            )
            check(
                "Publication race has continuous immutable revisions",
                sorted(d["revision"] for d in documents) == list(range(1, len(documents) + 1)),
                "Concurrent ingestion/publication can coalesce revisions but cannot create gaps or partial documents",
            )
            for document in documents:
                oracle = sql_totals(store, tenant_a, document["known_at"])
                assert document["total_units"] == oracle["units"]
                assert document["amount_microusd"] == oracle["amount_microusd"]
                assert len(document["lines"]) == oracle["events"]
            check(
                "Every HTTP revision independently reconciles at its cutoff",
                True,
                "Separately written SQL oracle verifies event count, units and integer amount for every observed revision",
            )
            with store.tenant(tenant_a) as conn:
                winner = conn.execute("SELECT units FROM usage_events WHERE id='conflict'").fetchone()[
                    "units"
                ]
                foreign = conn.execute(
                    "SELECT count(*) AS n FROM usage_events WHERE tenant_id<>%s", (tenant_a,)
                ).fetchone()["n"]
            check(
                "Final HTTP amount matches fixture arithmetic",
                final["total_units"] == 100 + winner + 20 + sum(range(1, 16, 2))
                and final["amount_microusd"] == final["total_units"] * 250,
                "All usage is before the October 15 price boundary; 250 micro-USD per unit",
            )
            check(
                "Restricted SQL still enforces isolation",
                foreign == 0
                and len(store.snapshot(tenant_b)[0]) == 1
                and sum(e["units"] for e in store.snapshot(tenant_c)[0]) == 0,
                "No foreign ledger rows; account B contains only its independent reused ID; account C balance is zero",
            )

            before = len(store.snapshot(tenant_a)[2])
            malformed = [
                product('{"x":NaN}'),
                product('{"x":Infinity}'),
                product('{"x":1e400}'),
                product('{"x":"\\u0000"}'),
                product('{"x":"\\ud800"}'),
                product('{"x":1,"x":2}'),
                b"[" * 65 + b"0" + b"]" * 65,
                b'{"x":',
            ]
            responses = [
                request(
                    client,
                    "unsupported-json",
                    "POST",
                    "/api/events",
                    token_a,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                for body in malformed
            ]
            check(
                "Unsupported JSON fails safely before writes",
                all(r.status_code == 422 for r in responses) and len(store.snapshot(tenant_a)[2]) == before,
                "Non-finite/overflow numbers, NUL, invalid Unicode, duplicate keys, excessive depth and malformed syntax return 422 with no product writes",
            )
            for content in [b"x" * 8193, (b"x" * 4096 for _ in range(3))]:
                response = request(client, "oversize", "POST", "/api/events", token_a, content=content)
                assert response.status_code == 413
            check(
                "Fixed-length and chunked limits preserve headers",
                True,
                "Both 413 responses carry no-store, nosniff and CSP",
            )

            for token in [None, "invalid-disposable-token", token_a + "changed"]:
                assert request(client, "invalid-auth", "GET", "/api/statements", token).status_code == 401
            with store.admin() as conn:
                conn.execute(
                    "UPDATE tenants SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                    (tenant_b,),
                )
            response = request(client, "expired-auth", "GET", "/api/statements", token_b)
            check(
                "Missing, invalid, altered and expired tokens rejected",
                response.status_code == 401,
                "Expiry is changed only by fixture administration; the expired token is checked through the real endpoint",
            )
            report["ledger"] = {
                "account_a_revisions": len(documents),
                "account_a_units": final["total_units"],
                "account_a_amount_microusd": final["amount_microusd"],
                "account_c_balance": 0,
            }

        # A fresh single process exercises the limiter without disabling or raising it.
        with server(Path.cwd(), url) as client:
            started = time.monotonic()
            responses = [request(client, "rate-limit", "GET", "/api/overview") for _ in range(241)]
            check(
                "Unmodified process limiter rejects request 241",
                time.monotonic() - started < 60
                and Counter(r.status_code for r in responses) == {401: 240, 429: 1}
                and responses[-1].headers.get("retry-after") == "60",
                "Fresh Uvicorn process, 240 permitted unauthenticated requests then one 429; early error headers also verified",
            )
        current = [r for r in records if not r["scenario"].startswith("baseline-")]
        durations = [r["seconds"] for r in current if r["scenario"] != "rate-limit"]
        report["summary"] = {
            "current_requests": len(current),
            "current_status_counts": dict(sorted(Counter(str(r["status"]) for r in current).items())),
            "current_5xx": sum(r["status"] >= 500 for r in current),
            "scenario_status_counts": {
                name: dict(
                    sorted(Counter(str(r["status"]) for r in records if r["scenario"] == name).items())
                )
                for name in sorted({r["scenario"] for r in records})
            },
            "latency_excluding_sequential_limiter_probe": {
                "p50_seconds": round(statistics.median(durations), 6),
                "p95_seconds": quantile(durations, 0.95),
                "max_seconds": max(durations),
                "scope": "Observed bounded loopback HTTP exercise including token lookup and connection setup, not throughput capacity",
            },
        }
        check(
            "Current release produced no HTTP 5xx",
            report["summary"]["current_5xx"] == 0,
            "Intentionally reproduced baseline failures are excluded and reported separately",
        )
        with store.admin() as conn:
            report["postgresql"] = conn.execute("SELECT version() AS version").fetchone()["version"]
        report["source_files"] = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(name)
                for name in [
                    "app.py",
                    "store.py",
                    "billing.py",
                    "schema.sql",
                    "verify_http.py",
                    "verify_workload.py",
                    "tests/test_http_boundary.py",
                    ".github/workflows/ci.yml",
                ]
            ]
        }
    finally:
        with store.admin() as conn:
            for tenant_id in created:
                conn.execute("DELETE FROM tenants WHERE id=%s", (tenant_id,))
        report["fixture_cleanup"] = (
            "Removed only sandbox accounts created by this exercise; Uvicorn children stopped"
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "passed": len(checks), "summary": report["summary"]}))


if __name__ == "__main__":
    main()
