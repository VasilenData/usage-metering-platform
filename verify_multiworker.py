"""Bounded synthetic multi-process HTTP, response-loss and admission experiment.

The test-only ASGI wrapper can withhold one response after the real route returns.
It is never imported by app.py. Production code has no fault-control endpoint.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import socket
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
from psycopg.conninfo import make_conninfo

from bootstrap_runtime import configure_runtime
from runtime_store import RuntimeStore
from verify_http import usage
from verify_runtime_restore import fingerprint, fresh_database
from verify_workload import quantile

ROOT = Path(__file__).resolve().parent
BASELINE = "d4a9d852ea2bfe7c28589daea9a5a946f07279bf"
PASSWORD = "ci-runtime-disposable-only-20261113"


class HoldResponse:
    def __init__(self, application, marker):
        self.application, self.marker = application, marker

    async def __call__(self, scope, receive, send):
        selected = scope["type"] == "http" and (b"x-exercise-hold", b"after-commit") in scope["headers"]

        async def controlled_send(message):
            if selected and message["type"] == "http.response.start":
                # Route completed, but no HTTP headers/body have been transmitted.
                self.marker.write_text(str(os.getpid()))
                await asyncio.sleep(30)
                raise RuntimeError("Fault supervisor did not terminate the selected worker")
            await send(message)

        await self.application(scope, receive, controlled_send)


@contextmanager
def worker(runtime_url, marker):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DATABASE_URL",
            "ADMIN_DATABASE_URL",
            "TEST_DATABASE_URL",
            "METER_RUNTIME_PASSWORD",
            "PGPASSWORD",
        }
    }
    environment["METER_RUNTIME_DATABASE_URL"] = runtime_url
    environment["METER_EXERCISE_MARKER"] = str(marker)
    # Windows venv redirectors can make Popen.pid identify a launcher rather
    # than the serving interpreter. Launch the actual interpreter, retaining
    # this environment's packages, so observed PID is the process we kill.
    environment["PYTHONPATH"] = os.pathsep.join([str(ROOT), *[p for p in sys.path if "site-packages" in p]])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        child = subprocess.Popen(
            [sys._base_executable, str(Path(__file__).resolve()), "--serve", str(port)],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
                for _ in range(150):
                    if child.poll() is not None:
                        raise RuntimeError("Exercise worker exited; private log withheld")
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Exercise worker readiness timeout")
                yield child, client
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def baseline_probe(admin_url):
    """Export every pinned file byte; run the original HTTP path in new processes."""
    with tempfile.TemporaryDirectory(prefix="meter-baseline-") as directory:
        checkout = Path(directory)
        names = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", BASELINE], cwd=ROOT, text=True
        ).splitlines()
        for name in names:
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(["git", "show", BASELINE + ":" + name], cwd=ROOT))
        script = """
import json, os, time
from pathlib import Path
from bootstrap_runtime import bootstrap
from runtime_store import RuntimeStore
from psycopg.conninfo import make_conninfo
from verify_http import server
url=os.environ["TEST_DATABASE_URL"]; password="ci-runtime-disposable-only-20261113"
bootstrap(url,password)
runtime=make_conninfo(url,user="meter_api_runtime",password=password)
_,token=RuntimeStore(runtime).scoped().create_tenant()
headers={"Authorization":"Bearer "+token}; started=time.monotonic()
with server(Path.cwd(),runtime,runtime=True) as a, server(Path.cwd(),runtime,runtime=True) as b:
    statuses=[(a if i%2==0 else b).get("/api/statements",headers=headers).status_code for i in range(242)]
with server(Path.cwd(),runtime,runtime=True) as restarted:
    reset=restarted.get("/api/statements",headers=headers).status_code
elapsed=time.monotonic()-started
assert statuses.count(200)==240 and statuses.count(429)==2 and reset==200 and elapsed<60
print(json.dumps({"accepted":240,"rejected":2,"restarted_status":reset,"elapsed_seconds":elapsed}))
"""
        environment = dict(os.environ, TEST_DATABASE_URL=admin_url)
        process = subprocess.run(
            [sys.executable, "-c", script],
            cwd=checkout,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if process.returncode:
            raise RuntimeError("Pinned baseline counterexample failed; private output withheld")
        return {"commit": BASELINE, "verified_exported_files": len(names), **json.loads(process.stdout)}


class PressureSampler:
    def __init__(self, url):
        self.url, self.samples, self.stop = url, [], threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.error = None

    def run(self):
        try:
            with psycopg.connect(self.url, autocommit=True) as conn:
                while not self.stop.is_set():
                    row = conn.execute("""SELECT count(*), count(*) FILTER(WHERE state='active'),
                        count(*) FILTER(WHERE wait_event_type='Lock') FROM pg_stat_activity
                        WHERE datname=current_database() AND usename='meter_api_runtime'""").fetchone()
                    self.samples.append(row)
                    self.stop.wait(0.05)
        except Exception as error:
            self.error = type(error).__name__

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive() or self.error or not self.samples:
            raise RuntimeError("Database pressure sampling failed")
        return {
            "interval_seconds": 0.05,
            "samples": len(self.samples),
            "max_runtime_connections": max(row[0] for row in self.samples),
            "max_active_runtime_connections": max(row[1] for row in self.samples),
            "max_lock_waiters": max(row[2] for row in self.samples),
            "samples_with_lock_waiters": sum(row[2] > 0 for row in self.samples),
            "scope": "sampled pg_stat_activity on this isolated database; peaks between samples can be missed",
        }


def database_counters(url):
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute("""SELECT xact_commit,xact_rollback,blks_read,blks_hit,temp_bytes,deadlocks
            FROM pg_stat_database WHERE datname=current_database()""").fetchone()
        return dict(
            zip(("commits", "rollbacks", "blocks_read", "blocks_hit", "temp_bytes", "deadlocks"), row)
        )


def sustained_load(url, clients, tokens, phase_seconds):
    samples = []
    sampler = PressureSampler(url)
    before = database_counters(url)
    sampler.thread.start()
    started = time.perf_counter()

    def request(index, rate, due):
        account = index % len(tokens)
        kind = index % 10
        method, path, payload = "GET", "/api/statements", None
        if kind < 7:
            method, path, payload = "POST", "/api/usage", usage(f"load-{index}", index % 9 + 1)
        elif kind == 9:
            method, path, payload = "POST", "/api/statements", {"month": "2026-10"}
        sent = time.perf_counter()
        response = clients[index % 2].request(
            method, path, json=payload, headers={"Authorization": "Bearer " + tokens[account]}
        )
        ended = time.perf_counter()
        return {
            "index": index,
            "worker": index % 2,
            "account": account,
            "method": method,
            "path": path,
            "target_rps": rate,
            "status": response.status_code,
            "latency_ms": round((ended - sent) * 1000, 3),
            "scheduling_lag_ms": round(max(0, sent - due) * 1000, 3),
            "accepted": response.json().get("accepted") if path == "/api/usage" else None,
        }

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            index, futures = 0, []
            for phase, rate in enumerate((5, 10, 20)):
                for position in range(rate * phase_seconds):
                    due = started + phase * phase_seconds + position / rate
                    time.sleep(max(0, due - time.perf_counter()))
                    futures.append(pool.submit(request, index, rate, due))
                    index += 1
            samples = [future.result() for future in futures]
    finally:
        pressure = sampler.finish()
    duration = time.perf_counter() - started
    # PostgreSQL cumulative statistics update asynchronously; this is not an exact
    # per-query profiler. Waiting is outside the request/latency measurement.
    time.sleep(1.1)
    after = database_counters(url)
    phases = []
    for rate in (5, 10, 20):
        phase = [sample for sample in samples if sample["target_rps"] == rate]
        phases.append(
            {
                "target_rps": rate,
                "offering_seconds": phase_seconds,
                "requests": len(phase),
                "statuses": dict(Counter(sample["status"] for sample in phase)),
                "p95_latency_ms": quantile([sample["latency_ms"] for sample in phase], 0.95),
                "max_scheduling_lag_ms": max(sample["scheduling_lag_ms"] for sample in phase),
            }
        )
    return {
        "duration_seconds": duration,
        "requests": len(samples),
        "client_concurrency_cap": 8,
        "completed_rps": len(samples) / duration,
        "worker_distribution": dict(Counter(s["worker"] for s in samples)),
        "status_distribution": dict(Counter(s["status"] for s in samples)),
        "latency_ms": {
            "p50": quantile([s["latency_ms"] for s in samples], 0.5),
            "p95": quantile([s["latency_ms"] for s in samples], 0.95),
            "p99": quantile([s["latency_ms"] for s in samples], 0.99),
            "max": max(s["latency_ms"] for s in samples),
        },
        "database_pressure": pressure,
        "database_counter_deltas": {key: after[key] - before[key] for key in before},
        "request_samples": samples,
        "phases": phases,
    }


def run(admin_url, phase_seconds):
    checks = []
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": os.getenv("GITHUB_SHA", "local-uncommitted"),
        "ci_run_id": os.getenv("GITHUB_RUN_ID"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "scope": "Synthetic local two-process Uvicorn; explicit client alternation, no network load balancer",
        "checks": checks,
    }

    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        assert condition, name

    with fresh_database(admin_url, closed=False) as url, tempfile.TemporaryDirectory() as private:
        report["baseline_counterexample"] = baseline_probe(url)
        before = fingerprint(url)
        configure_runtime(url, PASSWORD)  # upgrade the actual original schema/data
        check("Additive migration preserves every ledger/account column", fingerprint(url) == before)
        runtime_url = make_conninfo(url, user="meter_api_runtime", password=PASSWORD)
        runtime = RuntimeStore(runtime_url)
        marker = Path(private) / "committed.txt"
        with (
            worker(runtime_url, marker) as (process_a, client_a),
            worker(runtime_url, Path(private) / "unused") as (_, client_b),
        ):
            blocked_account, blocked_token = runtime.scoped().create_tenant()
            with psycopg.connect(url) as blocker:
                blocker.execute("SELECT pg_advisory_xact_lock(87326004,1)")
                started = time.monotonic()
                response = client_a.post(
                    "/api/usage",
                    json=usage("must-not-run", 1),
                    headers={"Authorization": "Bearer " + blocked_token},
                )
                elapsed = time.monotonic() - started
                check(
                    "Contended admission fails closed with bounded protected 503",
                    response.status_code == 503
                    and response.headers.get("Retry-After") == "2"
                    and response.headers.get("Cache-Control") == "no-store"
                    and response.headers.get("X-Content-Type-Options") == "nosniff"
                    and 1.8 <= elapsed < 8,
                )
            with psycopg.connect(url) as conn:
                check(
                    "Admission timeout cannot mutate ledger",
                    conn.execute(
                        "SELECT count(*) FROM usage_events WHERE tenant_id=%s", (blocked_account["id"],)
                    ).fetchone()[0]
                    == 0,
                )
            _, token = runtime.scoped().create_tenant()
            _, other = runtime.scoped().create_tenant()
            headers = {"Authorization": "Bearer " + token}
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=8) as pool:
                statuses = list(
                    pool.map(
                        lambda i: (
                            (client_a if i % 2 else client_b)
                            .get("/api/statements", headers=headers)
                            .status_code
                        ),
                        range(160),
                    )
                )
            check(
                "One shared account window across both workers",
                Counter(statuses) == {200: 120, 429: 40} and time.monotonic() - started < 60,
            )
            report["shared_account_quota"] = {
                "statuses": dict(Counter(statuses)),
                "elapsed_seconds": time.monotonic() - started,
            }
            check(
                "Noisy account cannot consume another account allowance",
                client_b.get("/api/statements", headers={"Authorization": "Bearer " + other}).status_code
                == 200,
            )
            started_anonymous = time.monotonic()
            with ThreadPoolExecutor(max_workers=8) as pool:
                anonymous = list(
                    pool.map(
                        lambda i: (
                            (client_a if i % 2 else client_b)
                            .get("/api/statements", headers={"X-Forwarded-For": str(i)})
                            .status_code
                        ),
                        range(260),
                    )
                )
            check(
                "Anonymous quota is shared and ignores forwarded header rotation",
                Counter(anonymous) == {401: 240, 429: 20} and time.monotonic() - started_anonymous < 60,
            )
            report["shared_anonymous_quota"] = {
                "statuses": dict(Counter(anonymous)),
                "elapsed_seconds": time.monotonic() - started_anonymous,
            }
            account, fault_token = runtime.scoped().create_tenant()
            fault_headers = {"Authorization": "Bearer " + fault_token}
            for i in range(6):
                check(
                    f"Acknowledged seed request {i}",
                    (client_a if i % 2 else client_b)
                    .post("/api/usage", json=usage(f"acked-{i}", 10), headers=fault_headers)
                    .status_code
                    == 200,
                )
            event = usage("ambiguous-commit", 17)

            def uncertain_request():
                try:
                    client_a.post(
                        "/api/usage", json=event, headers={**fault_headers, "X-Exercise-Hold": "after-commit"}
                    )
                except httpx.TransportError:
                    return "transport_error_no_response"
                return "unexpected_response"

            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(uncertain_request)
                deadline = time.monotonic() + 15
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                check(
                    "Response withheld by actual selected worker",
                    marker.exists() and int(marker.read_text()) == process_a.pid,
                )
                with psycopg.connect(url) as conn:
                    count = conn.execute(
                        "SELECT count(*) FROM usage_events WHERE tenant_id=%s AND id='ambiguous-commit'",
                        (account["id"],),
                    ).fetchone()[0]
                check("Independent connection observes committed row before worker kill", count == 1)
                process_a.kill()
                process_a.wait(timeout=10)
                check(
                    "Client received no success response",
                    pending.result(timeout=10) == "transport_error_no_response",
                )
            replay = client_b.post("/api/usage", json=event, headers=fault_headers)
            check(
                "Retry through survivor identifies duplicate",
                replay.status_code == 200 and replay.json()["duplicate"] and not replay.json()["accepted"],
            )
            statement = client_b.post(
                "/api/statements", json={"month": "2026-10"}, headers=fault_headers
            ).json()
            with psycopg.connect(url) as conn:
                ids = conn.execute(
                    "SELECT id FROM usage_events WHERE tenant_id=%s", (account["id"],)
                ).fetchall()
            check(
                "No acknowledged event lost and no retry double charge",
                len(ids) == 7 and statement["total_units"] == 77 and statement["amount_microusd"] == 19250,
            )
            report["response_loss"] = {
                "committed_before_kill": True,
                "client_outcome": "transport_error_no_response",
                "surviving_events": 7,
                "total_units": 77,
                "amount_microusd": 19250,
            }
            with worker(runtime_url, Path(private) / "restarted") as (_, restarted):
                # This assertion deliberately fails if the exercise has run past
                # the window; do not weaken it to make slow hardware look correct.
                check(
                    "Restart does not reset exhausted account",
                    restarted.get("/api/statements", headers=headers).status_code == 429,
                )
                accounts = [runtime.scoped().create_tenant() for _ in range(6)]
                report["load"] = sustained_load(
                    url, (restarted, client_b), [token for _, token in accounts], phase_seconds
                )
                check(
                    "Bounded offered load completed without HTTP/server errors",
                    set(report["load"]["status_distribution"]) == {200},
                )
                check(
                    "No observed database deadlocks during bounded load",
                    report["load"]["database_counter_deltas"]["deadlocks"] == 0,
                )
                for index, (tenant, _) in enumerate(accounts):
                    samples = [
                        s
                        for s in report["load"]["request_samples"]
                        if s["account"] == index and s["path"] == "/api/usage"
                    ]
                    expected = sum(s["index"] % 9 + 1 for s in samples)
                    with psycopg.connect(url) as conn:
                        observed = conn.execute(
                            "SELECT count(*),coalesce(sum(units),0) FROM usage_events WHERE tenant_id=%s",
                            (tenant["id"],),
                        ).fetchone()
                    check(
                        f"Load account {index}: every acknowledged event exists once",
                        observed == (len(samples), expected) and all(s["accepted"] for s in samples),
                    )
                    statement = client_b.post(
                        "/api/statements",
                        json={"month": "2026-10"},
                        headers={"Authorization": "Bearer " + accounts[index][1]},
                    ).json()
                    check(
                        f"Load account {index}: final charge reconciles independently",
                        statement["total_units"] == expected
                        and statement["amount_microusd"] == expected * 250,
                    )
        with psycopg.connect(url) as conn:
            report["postgresql"] = conn.execute("SELECT version()").fetchone()[0]
            report["admission_state"] = {
                "rows": conn.execute("SELECT count(*) FROM meter_api.admission_windows").fetchone()[0],
                "max_hits": conn.execute(
                    "SELECT max(cardinality(hits)) FROM meter_api.admission_windows"
                ).fetchone()[0],
                "relation_bytes": conn.execute(
                    "SELECT pg_total_relation_size('meter_api.admission_windows')"
                ).fetchone()[0],
            }
    report["source_files"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "app.py",
            "runtime_store.py",
            "runtime_schema.sql",
            "rate_limits.py",
            "store.py",
            "billing.py",
            "bootstrap_runtime.py",
            "verify_multiworker.py",
            "verify_runtime_restore.py",
            "verify_http.py",
            "verify_workload.py",
            "requirements.txt",
            "requirements-dev.txt",
            ".github/workflows/ci.yml",
        )
    }
    report["limitations"] = [
        "Two independent workers on one local host/database; no internet, proxy, production SLA or adoption evidence.",
        "Authentication and database connection creation occur before admission; quotas do not protect authentication CPU/connection costs.",
        "Per-kind advisory lock intentionally serializes admission; throughput is bounded by PostgreSQL and the measured fixture.",
        "Only logical state is bounded (256 identities per kind,120/240 timestamps); PostgreSQL MVCC still requires vacuum and disk monitoring.",
        "Abrupt API worker death after observed database commit is exercised; no database crash, failover or power-loss claim.",
        "Database clock controls rolling windows; a large clock jump changes expiry behavior.",
        "Pressure values are 50ms samples and asynchronously refreshed database counters, not exhaustive profiling.",
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=int)
    parser.add_argument("--output", default="local-evidence/multiworker-ci.json")
    parser.add_argument("--phase-seconds", type=int, default=10)
    args = parser.parse_args()
    if args.serve:
        import uvicorn

        from app import app

        uvicorn.run(
            HoldResponse(app, Path(os.environ["METER_EXERCISE_MARKER"])),
            host="127.0.0.1",
            port=args.serve,
            access_log=False,
            proxy_headers=False,
        )
        return
    if not 5 <= args.phase_seconds <= 15:
        parser.error("Use 5 to 15 seconds per bounded load phase")
    output = Path(args.output)
    if output.exists():
        parser.error("Choose a new evidence path; existing reports are immutable")
    report = run(os.environ["TEST_DATABASE_URL"], args.phase_seconds)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"checks": len(report["checks"]), "requests": report["load"]["requests"], "output": str(output)}
        )
    )


if __name__ == "__main__":
    main()
