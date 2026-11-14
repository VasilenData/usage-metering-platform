"""Fresh-database recovery of the actual restricted HTTP runtime; synthetic only."""

import argparse
import hashlib
import json
import os
import platform
import secrets
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from billing import canonical, digest
from bootstrap_runtime import bootstrap, configure_runtime
from demo import usage
from runtime_store import RuntimeStore
from verify_http import server
from verify_workload import sql_totals

ROOT = Path(__file__).resolve().parent
TABLES = ("tenants", "usage_events", "prices", "product_events", "statements")


@contextmanager
def fresh_database(admin_url, *, closed=True):
    """Only drop a generated database successfully created by this invocation."""
    if not conninfo_to_dict(admin_url).get("dbname", "").endswith("_test"):
        raise ValueError("Use a dedicated disposable PostgreSQL cluster and a _test database")
    name = "meter_runtime_restore_" + uuid.uuid4().hex + "_test"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        try:
            if closed:
                conn.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
                )
            yield make_conninfo(admin_url, dbname=name)
        finally:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def fingerprint(admin_url):
    """Hash every column, including account metadata, without exporting its values."""
    with psycopg.connect(admin_url, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        result = {}
        for table in TABLES:
            rows = conn.execute(sql.SQL("SELECT * FROM public.{}").format(sql.Identifier(table))).fetchall()
            ordered = sorted(canonical(row) for row in rows)
            result[table] = {"rows": len(rows), "sha256": digest(ordered)}
    return result


def pg_command(tool, admin_url, arguments, pg_bin):
    info = conninfo_to_dict(admin_url)
    environment = dict(
        os.environ,
        PGHOST=info.get("host", "localhost"),
        PGPORT=info.get("port", "5432"),
        PGUSER=info.get("user", "postgres"),
        PGPASSWORD=info.get("password", ""),
        PGDATABASE=info["dbname"],
    )
    executable = str(Path(pg_bin) / tool) if pg_bin else tool
    # Never put a credential-bearing DSN in process arguments or public output.
    completed = subprocess.run([executable, *arguments], env=environment, capture_output=True)
    if completed.returncode:
        raise RuntimeError(f"{tool} failed with status {completed.returncode}; private output withheld")


def run(admin_url, *, pg_bin=""):
    checks, requests = [], []
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": os.getenv("GITHUB_SHA", "local-uncommitted"),
        "ci_run_id": os.getenv("GITHUB_RUN_ID"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "scope": "Synthetic fresh database on one dedicated PostgreSQL cluster; real loopback Uvicorn",
        "checks": checks,
        "http_requests": requests,
    }

    def check(name, condition, detail):
        checks.append({"name": name, "passed": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(name)

    def request(client, phase, label, method, path, status, **kwargs):
        response = client.request(method, path, **kwargs)
        requests.append({"phase": phase, "scenario": label, "status": response.status_code})
        if response.status_code != status:
            raise AssertionError(f"{phase}: {label}: expected {status}, got {response.status_code}")
        return response.json()

    with fresh_database(admin_url, closed=False) as source_url, fresh_database(admin_url) as target_url:
        source_password = secrets.token_urlsafe(32)
        bootstrap(source_url, source_password)
        source_runtime = make_conninfo(source_url, user="meter_api_runtime", password=source_password)
        with psycopg.connect(source_url) as conn:
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO meter_api_runtime").format(
                    sql.Identifier(conninfo_to_dict(source_url)["dbname"])
                )
            )
            report["postgresql"] = conn.execute("SELECT version()").fetchone()[0]
        # The initial API is stopped before backup; no source writes run during capture.
        with server(ROOT, source_runtime, runtime=True) as client:
            a, b, expired = [
                request(client, "source", "create account", "POST", "/api/sandbox", 201) for _ in range(3)
            ]
            headers_a = {"Authorization": "Bearer " + a["token"]}
            headers_b = {"Authorization": "Bearer " + b["token"]}
            for headers, event in [
                (headers_a, usage("a-original", 100)),
                (headers_b, usage("b-original", 17)),
            ]:
                request(
                    client,
                    "source",
                    "ingest original",
                    "POST",
                    "/api/usage",
                    200,
                    headers=headers,
                    json=event,
                )
            first = request(
                client,
                "source",
                "A original statement",
                "POST",
                "/api/statements",
                200,
                headers=headers_a,
                json={"month": "2026-10"},
            )
            request(
                client,
                "source",
                "A correction",
                "POST",
                "/api/usage",
                200,
                headers=headers_a,
                json=usage("a-correction", -20, correction_of="a-original"),
            )
            second = request(
                client,
                "source",
                "A revised statement",
                "POST",
                "/api/statements",
                200,
                headers=headers_a,
                json={"month": "2026-10"},
            )
            b_statement = request(
                client,
                "source",
                "B statement",
                "POST",
                "/api/statements",
                200,
                headers=headers_b,
                json={"month": "2026-10"},
            )
            check(
                "Fixture has independently calculable statements",
                (
                    first["total_units"],
                    first["amount_microusd"],
                    second["total_units"],
                    second["amount_microusd"],
                    b_statement["amount_microusd"],
                )
                == (100, 25000, 80, 20000, 4250),
                "A: 100 - 20 units at 250 micro-USD; B: 17 at 250",
            )
        with psycopg.connect(source_url) as conn:
            conn.execute(
                "UPDATE tenants SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (expired["tenant_id"],),
            )
        before = fingerprint(source_url)
        with tempfile.TemporaryDirectory(prefix="meter-runtime-restore-") as directory:
            archive = Path(directory) / "private.dump"
            started = time.perf_counter()
            pg_command(
                "pg_dump",
                source_url,
                ["--format=custom", "--no-owner", "--no-acl", "--file", str(archive)],
                pg_bin,
            )
            pg_command(
                "pg_restore",
                target_url,
                [
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    "--single-transaction",
                    "--dbname",
                    conninfo_to_dict(target_url)["dbname"],
                    str(archive),
                ],
                pg_bin,
            )
            report["archive"] = {
                "bytes": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            check(
                "Restored complete data before permission changes",
                fingerprint(target_url) == before,
                "All five tables, every column; only counts and digests leave the private exercise",
            )
            target_old_runtime = make_conninfo(target_url, user="meter_api_runtime", password=source_password)
            try:
                with psycopg.connect(target_old_runtime):
                    pass
            except psycopg.OperationalError:
                blocked = True
            else:
                blocked = False
            with psycopg.connect(target_url) as conn:
                lacks_connect = not conn.execute(
                    "SELECT has_database_privilege('meter_api_runtime',current_database(),'CONNECT')"
                ).fetchone()[0]
            check(
                "Unhardened restore denies runtime connection",
                blocked and lacks_connect,
                "New template0 database has no PUBLIC CONNECT; API remains stopped",
            )
            with psycopg.connect(target_url) as conn:
                wrong_owners = conn.execute(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='meter_api' AND pg_get_userbyid(p.proowner)<>'meter_control'"
                ).fetchone()[0]
            check(
                "No-owner restore actually loses reviewed function ownership",
                wrong_owners == 5,
                "Counterexample: five restored control functions belong to the restore administrator",
            )
            target_password = secrets.token_urlsafe(32)
            configure_runtime(target_url, target_password)
            check(
                "Access-only bootstrap preserves all restored data",
                fingerprint(target_url) == before,
                "No demo seed, Store initialization or financial-history rewrite in configure_runtime",
            )
            # Administrative inspection precedes the explicit local runtime admission.
            with psycopg.connect(target_url) as conn:
                functions = conn.execute(
                    "SELECT p.proname,p.prosecdef,p.proconfig,r.rolname,r.rolcanlogin,r.rolsuper "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner "
                    "WHERE n.nspname='meter_api' ORDER BY p.proname"
                ).fetchall()
                public_execute = conn.execute(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a "
                    "WHERE n.nspname='meter_api' AND a.grantee=0 AND a.privilege_type='EXECUTE'"
                ).fetchone()[0]
                check(
                    "Restored control ownership and PUBLIC function ACLs hardened",
                    len(functions) == 5
                    and public_execute == 0
                    and all(
                        f[1:] == (True, ["search_path=pg_catalog"], "meter_control", False, False)
                        for f in functions
                    ),
                    "Five fixed SECURITY DEFINER functions; NOLOGIN owner; no PUBLIC execution",
                )
                conn.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO meter_api_runtime").format(
                        sql.Identifier(conninfo_to_dict(target_url)["dbname"])
                    )
                )
            target_runtime = make_conninfo(target_url, user="meter_api_runtime", password=target_password)
            runtime = RuntimeStore(target_runtime)
            check(
                "Current runtime startup gate passes after hardening",
                True,
                "Actual current role/ACL/function/RLS verification; administrative DSN not given to API",
            )
            # Direct SQL remains tenant-scoped even when the old tenant selector is forged.
            with runtime.connection() as conn:
                conn.execute("SELECT set_config('app.bearer_token',%s,true)", (a["token"],))
                conn.execute("SELECT set_config('app.tenant_id',%s,true)", (b["tenant_id"],))
                rows = conn.execute("SELECT DISTINCT tenant_id FROM public.usage_events").fetchall()
            check(
                "Restored SQL rejects forged account selection",
                rows == [{"tenant_id": a["tenant_id"]}],
                "A bearer plus B UUID still reads only A; query deliberately omits tenant filter",
            )
            for label, query, parameters in [
                (
                    "foreign account insert",
                    "INSERT INTO usage_events(tenant_id,source,id,occurred_at,units,payload_sha256) VALUES(%s,'x','intrusion',now(),1,'x')",
                    (b["tenant_id"],),
                ),
                ("price write", "UPDATE public.prices SET unit_price_microusd=0", None),
                ("role creation", "CREATE ROLE meter_restore_forbidden", None),
                ("assume control owner", "SET ROLE meter_control", None),
                ("read token hashes", "SELECT token_hash FROM public.tenants", None),
            ]:
                try:
                    with runtime.connection() as conn:
                        conn.execute("SELECT set_config('app.bearer_token',%s,true)", (a["token"],))
                        conn.execute(query, parameters)
                except psycopg.errors.InsufficientPrivilege:
                    denied = True
                else:
                    denied = False
                check("Restored runtime denied: " + label, denied, "Actual PostgreSQL insufficient_privilege")

            with server(ROOT, target_runtime, runtime=True) as client:
                restored_a = request(
                    client, "restored", "A statements", "GET", "/api/statements", 200, headers=headers_a
                )
                restored_b = request(
                    client, "restored", "B statements", "GET", "/api/statements", 200, headers=headers_b
                )
                check(
                    "HTTP retains original immutable statements",
                    restored_a == [second, first] and restored_b == [b_statement],
                    "Original IDs, revisions, cutoffs and contents are unchanged; accounts remain separate",
                )
                forged = request(
                    client,
                    "restored",
                    "forged HTTP account query",
                    "GET",
                    "/api/statements?tenant_id=" + b["tenant_id"],
                    200,
                    headers=headers_a,
                )
                check(
                    "HTTP account query cannot cross restored accounts",
                    forged == restored_a,
                    "Request identity remains token-derived",
                )
                request(
                    client,
                    "restored",
                    "expired account",
                    "GET",
                    "/api/statements",
                    401,
                    headers={"Authorization": "Bearer " + expired["token"]},
                )
                check(
                    "Expired bearer is not revived by restore",
                    True,
                    "Retained expiry rejects actual HTTP request",
                )
                replay = request(
                    client,
                    "restored",
                    "retry old usage",
                    "POST",
                    "/api/usage",
                    200,
                    headers=headers_a,
                    json=usage("a-original", 100),
                )
                unchanged = request(
                    client,
                    "restored",
                    "retry statement",
                    "POST",
                    "/api/statements",
                    200,
                    headers=headers_a,
                    json={"month": "2026-10"},
                )
                check(
                    "Post-restore retries preserve statement identity",
                    replay["duplicate"] and unchanged == second,
                    "Retry does not add usage or a financial revision",
                )
                request(
                    client,
                    "restored",
                    "foreign correction",
                    "POST",
                    "/api/usage",
                    422,
                    headers=headers_a,
                    json=usage("foreign-correction", -1, correction_of="b-original"),
                )
                request(
                    client,
                    "restored",
                    "late usage",
                    "POST",
                    "/api/usage",
                    200,
                    headers=headers_a,
                    json=usage("a-late", 40),
                )
                third = request(
                    client,
                    "restored",
                    "new revision",
                    "POST",
                    "/api/statements",
                    200,
                    headers=headers_a,
                    json={"month": "2026-10"},
                )
                final_a = request(
                    client, "restored", "complete A history", "GET", "/api/statements", 200, headers=headers_a
                )
                final_b = request(
                    client, "restored", "B unchanged", "GET", "/api/statements", 200, headers=headers_b
                )
                check(
                    "Restored API publishes a valid new revision",
                    third["revision"] == 3
                    and third["total_units"] == 120
                    and third["amount_microusd"] == 30000
                    and final_a == [third, second, first]
                    and final_b == [b_statement],
                    "Independent fixture total (100 - 20 + 40) * 250 = 30,000 micro-USD; B unchanged",
                )
            own_a = runtime.scoped(a["token"])
            for statement in final_a:
                reference = sql_totals(own_a, a["tenant_id"], statement["known_at"])
                check(
                    "Restored revision reconciles to independent SQL: " + str(statement["revision"]),
                    reference["units"] == statement["total_units"]
                    and reference["amount_microusd"] == statement["amount_microusd"],
                    "Separately written relational oracle at each immutable statement knowledge cutoff",
                )
            # A second fresh process exercises restart, rather than only first restored startup.
            with server(ROOT, target_runtime, runtime=True) as client:
                restarted = request(
                    client,
                    "restart",
                    "A history survives process restart",
                    "GET",
                    "/api/statements",
                    200,
                    headers=headers_a,
                )
                check(
                    "Second runtime restart retains published history",
                    restarted == final_a,
                    "Separate Uvicorn process, same restricted DSN; no administrative bootstrap at HTTP startup",
                )
            report["recovery_seconds"] = round(time.perf_counter() - started, 6)
            report["data_before_restore"] = before
            report["source_unchanged"] = fingerprint(source_url) == before
            check(
                "Source database remains unchanged",
                report["source_unchanged"],
                "Target-only replays/corrections; source five-table digests unchanged",
            )
    report["source_files"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in [
            "verify_runtime_restore.py",
            "bootstrap_runtime.py",
            "runtime_schema.sql",
            "runtime_store.py",
            "store.py",
            "schema.sql",
            "app.py",
            "billing.py",
            "demo.py",
            "verify_http.py",
            "verify_workload.py",
            ".github/workflows/ci.yml",
        ]
    }
    report["limitations"] = [
        "Trusted synthetic archive, fresh databases on the same dedicated PostgreSQL cluster; not a cross-cluster role migration.",
        "Runtime login password is reprovisioned cluster-wide; never run on a cluster shared with active Meterwork applications.",
        "One local HTTP process; no TLS, public traffic, point-in-time/WAL recovery, power-loss or service availability claim.",
        "Recovery time includes backup, permission hardening and assertions; one controlled run, not a recovery SLA.",
        "Only application data at backup time is recovered. Later revocations/writes require a separately governed recovery policy.",
        "Implementation is AI-assisted; no external security review, independent owner performance or real customer adoption implied.",
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", default="")
    parser.add_argument("--output", default="local-evidence/runtime-restore-ci.json")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise SystemExit("Choose a new evidence path; existing evidence is immutable")
    result = run(os.environ["TEST_DATABASE_URL"], pg_bin=args.pg_bin)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "passed": len(result["checks"]),
                "http_requests": len(result["http_requests"]),
            }
        )
    )


if __name__ == "__main__":
    main()
