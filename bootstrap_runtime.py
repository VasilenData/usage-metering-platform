"""Explicit administrative bootstrap. Never imported by the HTTP application."""

import os
from pathlib import Path

from psycopg import sql

from demo import seed
from store import Store


def bootstrap(admin_url, runtime_password):
    if not isinstance(runtime_password, str) or len(runtime_password) < 24:
        raise ValueError("Use a runtime password of at least 24 characters")
    store = Store(admin_url)
    seed(store)
    with store.admin() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(87326003)")
        for role, login in (("meter_control", False), ("meter_api_runtime", True)):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN"))
                )
            flags = conn.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
            if (
                any(flags[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls"))
                or flags["rolcanlogin"] != login
            ):
                raise ValueError("Existing role has unexpected privilege flags; review it explicitly")
            if conn.execute(
                "SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)",
                (role,),
            ).fetchone():
                raise ValueError("Dedicated runtime/control role must have no role memberships")
        conn.execute(
            sql.SQL("ALTER ROLE meter_api_runtime PASSWORD {}").format(sql.Literal(runtime_password))
        )
        conn.execute(Path(__file__).with_name("runtime_schema.sql").read_text(encoding="utf-8"))
    return store


if __name__ == "__main__":
    bootstrap(os.environ["ADMIN_DATABASE_URL"], os.environ["METER_RUNTIME_PASSWORD"])
    print("Administrative bootstrap completed; no credential values emitted")
