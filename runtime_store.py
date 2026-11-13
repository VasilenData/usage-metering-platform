"""Credential-separated request path with token-derived PostgreSQL row scope."""

import hashlib
import secrets
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from store import Capacity, Store


class RuntimeStore(Store):
    def __init__(self, runtime_url, *, verify=True):
        # Do not invoke Store.__init__: it is administrative offline tooling.
        self.runtime_url = runtime_url
        self.token = None
        self.tenant_id = None
        self.demo_read = False
        if verify:
            with self.connection() as conn:
                role = conn.execute(
                    "SELECT current_user AS name,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ).fetchone()
                if role["name"] != "meter_api_runtime" or any(
                    role[k] for k in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls")
                ):
                    raise ValueError("HTTP runtime requires the dedicated restricted meter_api_runtime login")
                if conn.execute(
                    "SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
                ).fetchone():
                    raise ValueError("HTTP runtime must not inherit or assume another role")
                if conn.execute(
                    "SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user) AND relnamespace='public'::regnamespace"
                ).fetchone():
                    raise ValueError("HTTP runtime must not own application relations")
                if conn.execute(
                    "SELECT has_schema_privilege(current_user,'public','CREATE') OR has_schema_privilege(current_user,'meter_api','CREATE') OR has_database_privilege(current_user,current_database(),'CREATE') AS unsafe"
                ).fetchone()["unsafe"]:
                    raise ValueError("HTTP runtime must not create database/schema objects")
                if conn.execute(
                    "SELECT has_any_column_privilege(current_user,'public.tenants','SELECT,INSERT,UPDATE,REFERENCES') OR has_table_privilege(current_user,'public.prices','INSERT,UPDATE,DELETE,TRUNCATE') AS unsafe"
                ).fetchone()["unsafe"]:
                    raise ValueError("HTTP runtime has unexpected control-plane table permissions")
                if conn.execute(
                    "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='meter_api' AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
                ).fetchone():
                    raise ValueError("HTTP runtime must not own control functions")
                enforced = conn.execute(
                    "SELECT count(*) AS n FROM pg_class WHERE oid IN ('public.usage_events'::regclass,'public.prices'::regclass,'public.statements'::regclass,'public.product_events'::regclass) AND relrowsecurity AND relforcerowsecurity"
                ).fetchone()["n"]
                if enforced != 4:
                    raise ValueError("HTTP runtime requires enforced row security on all ledger relations")
                if not conn.execute("SELECT * FROM meter_api.demo_metadata()").fetchone():
                    raise ValueError("Run the explicit administrative bootstrap before starting HTTP")

    def connection(self):
        return psycopg.connect(self.runtime_url, row_factory=dict_row, connect_timeout=10)

    def admin(self):
        raise PermissionError("Administrative operations are unavailable to the HTTP runtime")

    def provision_price(self, *args, **kwargs):
        raise PermissionError("Price provisioning belongs to the offline control plane")

    def scoped(self, token=None, *, demo=False):
        result = RuntimeStore(self.runtime_url, verify=False)
        result.token = token
        result.demo_read = demo
        result.tenant_id = "demo" if demo else None
        return result

    def authenticate(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            return None
        with self.connection() as conn:
            return conn.execute("SELECT * FROM meter_api.authenticate(%s)", (token,)).fetchone()

    @contextmanager
    def tenant(self, tenant_id):
        with self.connection() as conn:
            conn.execute("SET LOCAL statement_timeout='5s'")
            conn.execute("SELECT set_config('app.bearer_token',%s,true)", (self.token or "",))
            conn.execute("SELECT set_config('app.demo_read',%s,true)", ("on" if self.demo_read else "off",))
            current = conn.execute("SELECT meter_api.current_tenant() AS id").fetchone()["id"]
            if current != tenant_id and not (self.demo_read and tenant_id == "demo"):
                raise ValueError("Account scope is unavailable or expired")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (tenant_id,))
            yield conn

    def create_tenant(self, *, demo=False):
        if demo or self.token is not None or self.demo_read:
            raise PermissionError("Runtime may provision only a fresh disposable sandbox")
        token = secrets.token_urlsafe(32)
        try:
            with self.connection() as conn:
                tenant = conn.execute(
                    "SELECT * FROM meter_api.create_sandbox(%s)",
                    (hashlib.sha256(token.encode()).hexdigest(),),
                ).fetchone()
        except psycopg.errors.RaiseException as error:
            raise Capacity("Demo capacity reached; try after a sandbox expires") from error
        self.token, self.tenant_id = token, tenant["id"]
        return tenant, token

    def describe(self, tenant_id):
        if self.demo_read and tenant_id == "demo":
            with self.connection() as conn:
                return conn.execute("SELECT * FROM meter_api.demo_metadata()").fetchone()
        result = self.authenticate(self.token)
        if not result or result["id"] != tenant_id:
            raise ValueError("Account scope is unavailable or expired")
        return result

    def _duplicate(self, tenant_id):
        with self.tenant(tenant_id) as conn:
            conn.execute("SELECT meter_api.count_duplicate()")
