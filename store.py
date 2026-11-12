"""PostgreSQL storage, bounded demo lifecycle, immutable tenant-scoped data."""

import hashlib
import secrets
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from billing import canonical, digest, rate, timestamp


class Conflict(ValueError):
    pass


class Capacity(ValueError):
    pass


class Store:
    def __init__(self, admin_url):
        self.admin_url = admin_url
        # Deterministic secret derivation supports restarts without rotating an
        # existing process's credential. The admin connection is control-plane only.
        credentials = conninfo_to_dict(admin_url)
        password = hashlib.sha256(
            canonical({key: credentials.get(key) for key in ("host", "port", "user", "password")}).encode()
            + b":meter-runtime-v1"
        ).hexdigest()
        self.runtime_url = make_conninfo(admin_url, user="meter_runtime", password=password)
        with self.admin() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(87326001)")
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='meter_runtime'").fetchone():
                conn.execute(
                    "CREATE ROLE meter_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                )
            conn.execute(sql.SQL("ALTER ROLE meter_runtime PASSWORD {}").format(sql.Literal(password)))
            conn.execute(Path(__file__).with_name("schema.sql").read_text())
            conn.execute("GRANT USAGE ON SCHEMA public TO meter_runtime")
            for table in ("usage_events", "prices", "statements", "product_events"):
                identifier = sql.Identifier(table)
                conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(identifier))
                conn.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(identifier))
                conn.execute(sql.SQL("DROP POLICY IF EXISTS tenant_scope ON {}").format(identifier))
                conn.execute(
                    sql.SQL(
                        "CREATE POLICY tenant_scope ON {} TO meter_runtime USING "
                        "(tenant_id = current_setting('app.tenant_id', true)) WITH CHECK "
                        "(tenant_id = current_setting('app.tenant_id', true))"
                    ).format(identifier)
                )
                conn.execute(sql.SQL("REVOKE ALL ON {} FROM meter_runtime").format(identifier))
                conn.execute(sql.SQL("GRANT SELECT, INSERT ON {} TO meter_runtime").format(identifier))
            # Prices are provisioned administratively and immutable to request code.
            conn.execute("REVOKE INSERT ON prices FROM meter_runtime")

    def admin(self):
        return psycopg.connect(self.admin_url, row_factory=dict_row, connect_timeout=10)

    @contextmanager
    def tenant(self, tenant_id):
        with psycopg.connect(self.runtime_url, row_factory=dict_row, connect_timeout=10) as conn:
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            # Serializes writes/publications only within one tenant; lock and reads
            # share READ COMMITTED transaction so waiting writers see committed data.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,))
            yield conn

    def create_tenant(self, *, demo=False):
        token = secrets.token_urlsafe(32)
        tenant_id = "demo" if demo else str(uuid.uuid4())
        with self.admin() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(87326002)")
            if demo:
                existing = conn.execute("SELECT * FROM tenants WHERE id='demo'").fetchone()
                if existing:
                    return existing, None
            else:
                # Only intentionally disposable sandbox data has this retention policy.
                conn.execute(
                    "DELETE FROM tenants WHERE scope='disposable-sandbox' AND expires_at < clock_timestamp()"
                )
                active = conn.execute(
                    "SELECT count(*) AS n FROM tenants WHERE scope='disposable-sandbox'"
                ).fetchone()["n"]
                if active >= 64:
                    raise Capacity("Demo capacity reached; try again after a sandbox expires")
            expires = None if demo else datetime.now(UTC) + timedelta(hours=1)
            tenant = conn.execute(
                "INSERT INTO tenants(id,name,token_hash,expires_at,scope) VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (
                    tenant_id,
                    "Northstar API · synthetic" if demo else "Your disposable sandbox",
                    hashlib.sha256(token.encode()).hexdigest(),
                    expires,
                    "synthetic-demo" if demo else "disposable-sandbox",
                ),
            ).fetchone()
            conn.execute(
                "INSERT INTO prices VALUES(%s,'standard-v1','2026-01-01T00:00:00Z',250),(%s,'standard-v2','2026-10-15T00:00:00Z',200)",
                (tenant_id, tenant_id),
            )
            return tenant, token

    def authenticate(self, token):
        if len(token) > 256:
            return None
        with self.admin() as conn:
            return conn.execute(
                "SELECT id,name,scope,expires_at,duplicate_events FROM tenants WHERE token_hash=%s "
                "AND scope='disposable-sandbox' AND expires_at > clock_timestamp()",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()

    def describe(self, tenant_id):
        with self.admin() as conn:
            return conn.execute(
                "SELECT id,name,scope,expires_at,duplicate_events FROM tenants WHERE id=%s", (tenant_id,)
            ).fetchone()

    def _duplicate(self, tenant_id):
        with self.admin() as conn:
            conn.execute("UPDATE tenants SET duplicate_events=duplicate_events+1 WHERE id=%s", (tenant_id,))

    def ingest_usage(self, tenant_id, body):
        body = dict(body)
        body["occurred_at"] = timestamp(body["occurred_at"])
        body.setdefault("correction_of", None)
        if type(body["units"]) is not int or not -100000 <= body["units"] <= 100000 or body["units"] == 0:
            raise ValueError("Units must be a non-zero integer bounded by 100,000")
        payload_hash = digest(body)
        duplicate = False
        with self.tenant(tenant_id) as conn:
            existing = conn.execute(
                "SELECT payload_sha256 FROM usage_events WHERE source=%s AND id=%s",
                (body["source"], body["id"]),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_hash:
                    raise Conflict("Event identity reused with different content")
                duplicate = True
            else:
                count = conn.execute("SELECT count(*) AS n FROM usage_events").fetchone()["n"]
                if count >= 500:
                    raise Capacity("Sandbox usage limit: 500 unique events")
                if body["correction_of"]:
                    original = conn.execute(
                        "SELECT * FROM usage_events WHERE source=%s AND id=%s",
                        (body["source"], body["correction_of"]),
                    ).fetchone()
                    if not original or original["correction_of"]:
                        raise ValueError(
                            "Correction requires an existing original in this account and source"
                        )
                    if original["occurred_at"] != body["occurred_at"]:
                        raise ValueError("Correction occurrence time must match original")
                    balance = conn.execute(
                        "SELECT sum(units) AS units FROM usage_events WHERE source=%s AND (id=%s OR correction_of=%s)",
                        (body["source"], body["correction_of"], body["correction_of"]),
                    ).fetchone()["units"]
                    if balance + body["units"] < 0:
                        raise ValueError("Correction would create negative usage")
                elif body["units"] < 0:
                    raise ValueError("Negative usage requires correction_of")
                conn.execute(
                    "INSERT INTO usage_events(tenant_id,source,id,occurred_at,units,correction_of,payload_sha256) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        tenant_id,
                        body["source"],
                        body["id"],
                        body["occurred_at"],
                        body["units"],
                        body["correction_of"],
                        payload_hash,
                    ),
                )
        if duplicate:
            self._duplicate(tenant_id)
        return {"id": body["id"], "accepted": not duplicate, "duplicate": duplicate}

    def ingest_product(self, tenant_id, body):
        body = dict(body)
        body["occurred_at"] = timestamp(body["occurred_at"])
        body.setdefault("properties", {})
        body.setdefault("experiment_id", None)
        body.setdefault("variant", None)
        if len(canonical(body).encode()) > 4096:
            raise ValueError("Product event exceeds 4KB")
        payload_hash = digest(body)
        with self.tenant(tenant_id) as conn:
            existing = conn.execute(
                "SELECT payload_sha256 FROM product_events WHERE event_id=%s", (body["event_id"],)
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_hash:
                    raise Conflict("Product event identity conflict")
                return {"accepted": False, "duplicate": True}
            if conn.execute("SELECT count(*) AS n FROM product_events").fetchone()["n"] >= 500:
                raise Capacity("Sandbox product-event limit: 500")
            conn.execute(
                "INSERT INTO product_events(tenant_id,event_id,actor_id,event_name,occurred_at,properties,experiment_id,variant,payload_sha256) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    tenant_id,
                    body["event_id"],
                    body["actor_id"],
                    body["event_name"],
                    body["occurred_at"],
                    Jsonb(body["properties"]),
                    body["experiment_id"],
                    body["variant"],
                    payload_hash,
                ),
            )
            return {"accepted": True, "duplicate": False}

    def snapshot(self, tenant_id):
        with self.tenant(tenant_id) as conn:
            usage = conn.execute("SELECT * FROM usage_events ORDER BY source,id").fetchall()
            prices = conn.execute("SELECT * FROM prices ORDER BY effective_from").fetchall()
            product = conn.execute("SELECT * FROM product_events ORDER BY received_at,event_id").fetchall()
            statements = conn.execute("SELECT document FROM statements ORDER BY created_at DESC").fetchall()
            return usage, prices, product, [row["document"] for row in statements]

    def publish(self, tenant_id, month, *, before_commit=None):
        with self.tenant(tenant_id) as conn:
            events = conn.execute("SELECT * FROM usage_events ORDER BY source,id").fetchall()
            prices = conn.execute("SELECT * FROM prices ORDER BY effective_from").fetchall()
            result = rate(events, prices, tenant_id=tenant_id, month=month)
            old = conn.execute(
                "SELECT document FROM statements WHERE month=%s AND content_sha256=%s",
                (month, result["content_sha256"]),
            ).fetchone()
            if old:
                return old["document"]
            revision = conn.execute(
                "SELECT coalesce(max(revision),0)+1 AS revision FROM statements WHERE month=%s", (month,)
            ).fetchone()["revision"]
            result.update(id=str(uuid.uuid4()), revision=revision, created_at=datetime.now(UTC).isoformat())
            conn.execute(
                "INSERT INTO statements(tenant_id,id,month,revision,document,content_sha256) VALUES(%s,%s,%s,%s,%s,%s)",
                (tenant_id, result["id"], month, revision, Jsonb(result), result["content_sha256"]),
            )
            if before_commit:
                before_commit(conn, result)
            return result
