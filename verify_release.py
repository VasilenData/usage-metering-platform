"""Reproduce process-interruption, backup/restore, and bounded load evidence.

Uses a dedicated disposable database ending in _test. Never accepts a production
database. Credentials stay in environment variables and are not included in output.
"""

import argparse
import hashlib
import json
import os
import platform
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from billing import canonical, digest
from demo import run_scenario, usage
from store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", default="")
    parser.add_argument("--output", default="local-evidence/release.json")
    args = parser.parse_args()
    url = os.environ["TEST_DATABASE_URL"]
    info = conninfo_to_dict(url)
    if not info.get("dbname", "").endswith("_test"):
        raise SystemExit("Only an explicitly disposable _test database is accepted")
    store = Store(url)
    results = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "scope": "synthetic controlled local PostgreSQL exercise",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "checks": [],
    }

    def check(name, passed, detail):
        results["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            raise AssertionError(name)

    for scenario in ("replay", "late", "pricing", "isolation"):
        result = run_scenario(store, scenario)
        results["checks"].extend(result["checks"])
    tenant, _ = store.create_tenant()
    tid = tenant["id"]
    store.ingest_usage(tid, usage("first", 100))
    original = store.publish(tid, "2026-10")
    store.ingest_usage(tid, usage("late", 40))
    environment = dict(os.environ, METER_TEST_TENANT=tid)
    child_code = """import os,sys
from store import Store
s=Store(os.environ['TEST_DATABASE_URL'])
def boundary(conn,document):
    print('INSERTED_UNCOMMITTED',flush=True)
    sys.stdin.read()
s.publish(os.environ['METER_TEST_TENANT'],'2026-10',before_commit=boundary)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        marker_queue = queue.Queue(maxsize=1)
        threading.Thread(target=lambda: marker_queue.put(child.stdout.readline()), daemon=True).start()
        marker = marker_queue.get(timeout=20)
        check(
            "Observed real uncommitted INSERT",
            marker.strip() == "INSERTED_UNCOMMITTED",
            "Subprocess signalled its transaction boundary before termination",
        )
        started = time.perf_counter()
        child.kill()
        child.wait(timeout=10)
        check(
            "Abrupt client death preserved published statement",
            store.snapshot(tid)[3] == [original],
            "Killed the Python publisher after INSERT; PostgreSQL rolled back the disconnected transaction",
        )
        recovered = store.publish(tid, "2026-10")
        results["client_crash_recovery_seconds"] = round(time.perf_counter() - started, 6)
        check(
            "Retry after abrupt death reconciles",
            recovered["revision"] == 2 and recovered["total_units"] == 140,
            "Two committed versions; no partial or phantom revision",
        )
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
    # A bounded concurrent-ingestion exercise, not a scale or distributed throughput claim.
    load_tenant, _ = store.create_tenant()
    durations = []

    def submit(index):
        started = time.perf_counter()
        result = store.ingest_usage(load_tenant["id"], usage(f"load-{index}", index + 1))
        return time.perf_counter() - started, result

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        for duration, response in pool.map(submit, range(200)):
            durations.append(duration)
            assert response["accepted"]
    elapsed = time.perf_counter() - started
    check(
        "Concurrent load conservation",
        store.publish(load_tenant["id"], "2026-10")["total_units"] == 20100,
        "200 unique events; units 1..200; independent arithmetic total 20,100",
    )
    results["load"] = {
        "events": 200,
        "concurrency": 8,
        "end_to_end_seconds": round(elapsed, 6),
        "observed_events_per_second": round(200 / elapsed, 3),
        "p50_seconds": round(statistics.median(durations), 6),
        "p95_seconds": round(sorted(durations)[189], 6),
        "scope": "local client + database; one tenant serialized by design; includes connection setup",
    }
    before = store.snapshot(tid)
    with tempfile.TemporaryDirectory(prefix="meterwork-restore-") as directory:
        dump = Path(directory) / "backup.dump"

        def executable(name):
            return str(Path(args.pg_bin) / name) if args.pg_bin else name

        pg_env = dict(
            os.environ,
            PGHOST=info.get("host", "localhost"),
            PGPORT=info.get("port", "5432"),
            PGUSER=info.get("user", "postgres"),
            PGPASSWORD=info.get("password", ""),
            PGDATABASE=info["dbname"],
        )
        started = time.perf_counter()
        subprocess.run(
            [executable("pg_dump"), "--format=custom", "--no-owner", "--file", str(dump)],
            env=pg_env,
            check=True,
            capture_output=True,
        )
        restore_name = "meterwork_restore_" + uuid.uuid4().hex[:10] + "_test"
        admin = psycopg.connect(url, autocommit=True)
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restore_name)))
            subprocess.run(
                [
                    executable("pg_restore"),
                    "--no-owner",
                    "--exit-on-error",
                    "--dbname",
                    restore_name,
                    str(dump),
                ],
                env=pg_env,
                check=True,
                capture_output=True,
            )
            restored = Store(make_conninfo(url, dbname=restore_name))
            after = restored.snapshot(tid)
            check(
                "Backup restoration preserves ledger and statements",
                digest(before) == digest(after),
                "pg_dump custom archive restored into a new disposable database; canonical snapshots match",
            )
            with restored.tenant(tid) as conn:
                check(
                    "Restored RLS still hides other accounts",
                    conn.execute(
                        "SELECT count(*) AS n FROM usage_events WHERE tenant_id <> %s", (tid,)
                    ).fetchone()["n"]
                    == 0,
                    "Restored database queried with restricted runtime identity",
                )
            results["backup_restore"] = {
                "seconds": round(time.perf_counter() - started, 6),
                "archive_bytes": dump.stat().st_size,
                "archive_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                "snapshot_sha256": digest(before),
                "scope": "entire disposable test database; synthetic data",
            }
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(restore_name))
            )
            admin.close()
    with store.admin() as conn:
        results["postgresql"] = conn.execute("SELECT version() AS version").fetchone()["version"]
    sources = list(Path(".").glob("*.py")) + [Path("schema.sql"), Path("requirements.txt")]
    sources += list(Path("web").glob("*")) + list(Path("tests").glob("test_*"))
    results["source_files"] = {
        path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(sources)
        if path.is_file()
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(
        canonical(
            {
                "passed": sum(row["passed"] for row in results["checks"]),
                "output": str(output),
                "load": results["load"],
            }
        )
    )


if __name__ == "__main__":
    main()
