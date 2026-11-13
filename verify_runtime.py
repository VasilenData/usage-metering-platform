"""Real PostgreSQL privilege and loopback HTTP fairness exercise, synthetic only."""

import argparse
import hashlib
import json
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from bootstrap_runtime import bootstrap
from demo import usage
from runtime_store import RuntimeStore
from verify_http import server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="local-evidence/runtime-ci.json")
    args = parser.parse_args()
    admin_url = os.environ["TEST_DATABASE_URL"]
    if not conninfo_to_dict(admin_url).get("dbname", "").endswith("_test"):
        raise SystemExit("Use a dedicated disposable database ending in _test")
    password = "ci-runtime-disposable-only-20261113"
    # Simulate schema drift before provisioning: it must not expose an unrelated
    # SECURITY DEFINER capability when the bootstrap is repeated.
    with psycopg.connect(admin_url) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS meter_api")
        conn.execute(
            "CREATE OR REPLACE FUNCTION meter_api.unrelated_helper_probe() RETURNS text "
            "LANGUAGE sql SECURITY DEFINER AS $$ SELECT 'not an HTTP capability'::text $$"
        )
    admin = bootstrap(admin_url, password)
    runtime_url = make_conninfo(admin_url, user="meter_api_runtime", password=password)
    runtime = RuntimeStore(runtime_url)
    a, b = runtime.scoped(), runtime.scoped()
    account_a, token_a = a.create_tenant()
    account_b, token_b = b.create_tenant()
    a.ingest_usage(account_a["id"], usage("only-a", 11))
    b.ingest_usage(account_b["id"], usage("only-b", 17))
    checks = []
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": os.getenv("GITHUB_SHA", "local-uncommitted"),
        "ci_run_id": os.getenv("GITHUB_RUN_ID"),
        "platform": platform.platform(),
        "scope": "Synthetic PostgreSQL privilege probes and one-process loopback HTTP; no external deployment",
        "checks": checks,
    }

    def check(name, condition, detail):
        checks.append({"name": name, "passed": bool(condition), "detail": detail})
        assert condition, name

    def denied(statement, parameters=None, token=None):
        try:
            with runtime.connection() as conn:
                if token:
                    conn.execute("SELECT set_config('app.bearer_token',%s,true)", (token,))
                conn.execute(statement, parameters)
        except psycopg.errors.InsufficientPrivilege:
            return True
        return False

    try:
        # Existing Store remains explicit admin/offline fixture tooling. This
        # counterexample demonstrates why its editable tenant GUC is not the API boundary.
        with psycopg.connect(admin.runtime_url) as conn:
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (account_b["id"],))
            rows = conn.execute("SELECT id FROM usage_events WHERE id='only-b'").fetchall()
        check(
            "Legacy selector alone can choose another tenant",
            rows == [("only-b",)],
            "Observed counterexample against the unchanged legacy offline Store role; not used by HTTP",
        )
        check(
            "HTTP Store contains no admin connection",
            not hasattr(runtime, "admin_url"),
            "Runtime receives only its own DSN",
        )
        try:
            RuntimeStore(admin_url)
        except ValueError:
            rejected = True
        else:
            rejected = False
        check("Startup rejects administrative identity", rejected, "Actual PostgreSQL role inspected")
        for name, grant, revoke in [
            (
                "replication role drift",
                "ALTER ROLE meter_api_runtime REPLICATION",
                "ALTER ROLE meter_api_runtime NOREPLICATION",
            ),
            (
                "PUBLIC truncate drift",
                "GRANT TRUNCATE ON public.usage_events TO PUBLIC",
                "REVOKE TRUNCATE ON public.usage_events FROM PUBLIC",
            ),
            (
                "account delete drift",
                "GRANT DELETE ON public.tenants TO meter_api_runtime",
                "REVOKE DELETE ON public.tenants FROM meter_api_runtime",
            ),
            (
                "unexpected helper drift",
                "GRANT EXECUTE ON FUNCTION meter_api.unrelated_helper_probe() TO meter_api_runtime",
                "REVOKE EXECUTE ON FUNCTION meter_api.unrelated_helper_probe() FROM meter_api_runtime",
            ),
        ]:
            with admin.admin() as conn:
                conn.execute(grant)
            try:
                try:
                    RuntimeStore(runtime_url)
                except ValueError:
                    rejected = True
                else:
                    rejected = False
            finally:
                with admin.admin() as conn:
                    conn.execute(revoke)
            check(
                "Startup rejects " + name,
                rejected,
                "Actual temporary administrative privilege change, then restored",
            )
        statements = {
            "create role": "CREATE ROLE meter_forbidden_probe",
            "create schema": "CREATE SCHEMA meter_forbidden_probe",
            "replace auth function": "CREATE OR REPLACE FUNCTION meter_api.current_tenant() RETURNS text LANGUAGE sql AS 'SELECT NULL::text'",
            "assume control role": "SET ROLE meter_control",
            "assume legacy role": "SET ROLE meter_runtime",
            "read token hashes": "SELECT token_hash FROM public.tenants",
            "invoke unrelated existing helper": "SELECT meter_api.unrelated_helper_probe()",
            "insert price": "INSERT INTO prices(tenant_id,id,effective_from,unit_price_microusd) VALUES('demo','forbidden','2026-01-01',0)",
            "update price": "UPDATE prices SET unit_price_microusd=0",
            "delete ledger": "DELETE FROM usage_events",
            "drop ledger": "DROP TABLE usage_events",
        }
        for name, query in statements.items():
            check(
                "Runtime denied: " + name,
                denied(query, token=token_a),
                "PostgreSQL insufficient_privilege, transaction rolled back",
            )
        with runtime.connection() as conn:
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (account_b["id"],))
            without_token = conn.execute("SELECT id FROM usage_events").fetchall()
            conn.execute("SELECT set_config('app.bearer_token',%s,true)", (token_a,))
            own_rows = conn.execute("SELECT id FROM usage_events ORDER BY id").fetchall()
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (account_b["id"],))
            guessed_rows = conn.execute("SELECT id FROM usage_events ORDER BY id").fetchall()
        check(
            "Tenant UUID cannot impersonate a bearer",
            without_token == [] and own_rows == guessed_rows == [{"id": "only-a"}],
            "Direct SQL changes the old selector before and after authenticating A; only A remains visible",
        )
        check(
            "Cross-account insert rejected by token-derived RLS",
            denied(
                "INSERT INTO usage_events(tenant_id,source,id,occurred_at,units,payload_sha256) VALUES(%s,'x','intrusion',now(),1,'x')",
                (account_b["id"],),
                token=token_a,
            ),
            "Valid A token cannot insert B data",
        )
        check(
            "Public demo remains read-only",
            denied(
                "INSERT INTO usage_events(tenant_id,source,id,occurred_at,units,payload_sha256) VALUES('demo','x','intrusion',now(),1,'x')"
            ),
            "No authenticated disposable tenant exists for the public demo",
        )
        with runtime.connection() as conn:
            conn.execute(
                "CREATE TEMP TABLE tenants(id text,name text,scope text,expires_at timestamptz,duplicate_events bigint,token_hash text)"
            )
            conn.execute(
                "INSERT INTO tenants VALUES('shadow','shadow','disposable-sandbox',now()+interval '1 hour',0,%s)",
                (hashlib.sha256(token_b.encode()).hexdigest(),),
            )
            real_identity = conn.execute("SELECT * FROM meter_api.authenticate(%s)", (token_b,)).fetchone()
            shadow = conn.execute(
                "SELECT * FROM meter_api.authenticate(%s)", ("x'); DROP TABLE public.tenants; --",)
            ).fetchall()
            functions = conn.execute(
                "SELECT p.proname,p.prosecdef,p.proconfig,r.rolcanlogin,r.rolsuper FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname='meter_api' AND has_function_privilege(current_user,p.oid,'EXECUTE')"
            ).fetchall()
            public_grants = conn.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE n.nspname='meter_api' AND a.grantee=0 AND a.privilege_type='EXECUTE'"
            ).fetchall()
        check(
            "Control functions resist temp shadow and token SQL syntax",
            shadow == [] and real_identity["id"] == account_b["id"],
            "A matching temporary token row cannot shadow public.tenants; SQL syntax in a parameter remains data",
        )
        check(
            "Control functions have restricted ownership and execution",
            len(functions) == 5
            and not public_grants
            and all(
                f["prosecdef"]
                and f["proconfig"] == ["search_path=pg_catalog"]
                and not f["rolcanlogin"]
                and not f["rolsuper"]
                for f in functions
            ),
            "Five fixed SECURITY DEFINER functions; NOLOGIN nonsuperuser owner; no PUBLIC execution",
        )

        with server(Path.cwd(), runtime_url, runtime=True) as client:
            started = time.monotonic()
            statuses = [
                client.get(
                    "/api/overview", headers={"X-Forwarded-For": f"198.51.100.{i % 250 + 1}"}
                ).status_code
                for i in range(241)
            ]
            check(
                "Forwarded-header rotation cannot reset anonymous allowance",
                statuses == [401] * 240 + [429] and time.monotonic() - started < 60,
                "241 requests from one real peer with rotating X-Forwarded-For; Uvicorn proxy headers disabled",
            )
            headers_a = {"Authorization": "Bearer " + token_a}
            statuses_a = [client.get("/api/statements", headers=headers_a).status_code for _ in range(121)]
            response_b = client.get("/api/statements", headers={"Authorization": "Bearer " + token_b})
            check(
                "Anonymous and account allowances remain separate",
                statuses_a == [200] * 120 + [429],
                "An exhausted anonymous peer bucket does not consume authenticated A's 120 requests",
            )
            check(
                "A noisy account cannot consume B's allowance",
                response_b.status_code == 200,
                "B succeeds from the same TCP peer after A is limited; no token/query/header-selected tenant key",
            )
            result["http"] = {
                "requests": 363,
                "anonymous": {"401": 240, "429": 1},
                "account_a": {"200": 120, "429": 1},
                "account_b": {"200": 1},
            }
        with admin.admin() as conn:
            conn.execute(
                "UPDATE tenants SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (account_a["id"],),
            )
        with runtime.connection() as conn:
            conn.execute("SELECT set_config('app.bearer_token',%s,true)", (token_a,))
            expired_rows = conn.execute("SELECT id FROM usage_events").fetchall()
        check(
            "Expired bearer loses SQL access",
            expired_rows == [] and runtime.authenticate(token_a) is None,
            "Expiry enforced in PostgreSQL, including direct SQL outside HTTP",
        )
        result["source_files"] = {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in [
                "app.py",
                "runtime_store.py",
                "runtime_schema.sql",
                "bootstrap_runtime.py",
                "rate_limits.py",
                "verify_runtime.py",
                "verify_http.py",
                ".github/workflows/ci.yml",
            ]
        }
        result["limitations"] = [
            "Bearer possession authorizes that account; app-host compromise can observe active bearers.",
            "One-process in-memory admission quotas, not a distributed gateway or denial-of-service guarantee.",
            "Anonymous clients behind one peer share its quota; sandbox capacity remains globally bounded to 64.",
            "Administrative fixture tools intentionally retain admin capabilities outside the HTTP process.",
            "Synthetic CI security checks are not an external penetration test or real adoption evidence.",
        ]
    finally:
        with admin.admin() as conn:
            conn.execute("DELETE FROM tenants WHERE id=ANY(%s)", ([account_a["id"], account_b["id"]],))
            conn.execute("DROP FUNCTION meter_api.unrelated_helper_probe()")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "passed": len(checks), "http": result.get("http")}))


if __name__ == "__main__":
    main()
