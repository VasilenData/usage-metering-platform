"""Real loopback API + independent PostgreSQL verification of the local starter."""

import hashlib
import json
import os
import platform
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from bootstrap_runtime import bootstrap
from starter.client import run
from verify_multiworker import worker
from verify_runtime_restore import fresh_database


def main():
    checks = []
    source_root = Path(__file__).resolve().parents[1]

    def source_hashes():
        paths = [
            *source_root.glob("*.py"),
            *source_root.glob("*.sql"),
            *(source_root / "starter").glob("*.py"),
        ]
        return {
            str(path.relative_to(source_root)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(paths)
        }

    source_identity = source_hashes()

    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})
        if not passed:
            raise AssertionError(name)

    with fresh_database(os.environ["TEST_DATABASE_URL"], closed=False) as url:
        with psycopg.connect(url) as conn:
            postgres_version = conn.execute("SHOW server_version").fetchone()[0]
        password = secrets.token_urlsafe(36)
        bootstrap(url, password)
        runtime_url = make_conninfo(url, user="meter_api_runtime", password=password)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".gitignore", ".dockerignore"):
                (root / name).write_text(".meterwork/\n", encoding="utf-8")
            with worker(runtime_url, root / "unused-marker") as (_, http):
                path, result = run(str(http.base_url).rstrip("/"), root=root)
                before = path.read_bytes()
                state = json.loads((path.parent / "state.json").read_text())
                check(
                    "first-use amounts reconcile",
                    result["reconciliation"]["delta_amount_microusd"] == -500000,
                )
                check(
                    "credentials excluded from result",
                    state["token"].encode() not in before and password.encode() not in before,
                )
                repeated_path, repeated = run(str(http.base_url).rstrip("/"), root=root)
                check("rerun is byte-identical", repeated_path.read_bytes() == before and repeated == result)
                with psycopg.connect(url) as conn:
                    totals = conn.execute(
                        "SELECT count(*),sum(units),sum(units*250) FROM usage_events WHERE tenant_id=%s",
                        (state["tenant_id"],),
                    ).fetchone()
                    check("independent SQL units and microUSD", totals == (2, 10000, 2500000))
                    statements = conn.execute(
                        "SELECT count(*) FROM statements WHERE tenant_id=%s", (state["tenant_id"],)
                    ).fetchone()[0]
                    check("only two immutable revisions", statements == 2)
                    conn.execute(
                        "UPDATE tenants SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                        (state["tenant_id"],),
                    )
                from starter.private import StarterError

                expired = False
                try:
                    run(str(http.base_url).rstrip("/"), root=root)
                except StarterError as error:
                    expired = "expired" in str(error)
                check("expired sandbox stops explicitly", expired)
                check("expired rerun preserves last complete export", path.read_bytes() == before)
    check("source bytes unchanged during the exercise", source_identity == source_hashes())
    report = {
        "scope": "Synthetic isolated PostgreSQL 17 and real loopback Uvicorn; Docker not exercised here",
        "checks": checks,
        "source_files": source_identity,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": os.environ.get("GITHUB_SHA", "local-uncommitted"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "postgresql": postgres_version,
        "reconciliation": result["reconciliation"],
    }
    destination = source_root / "local-evidence/starter.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks": len(checks), "passed": all(row["passed"] for row in checks)}))


if __name__ == "__main__":
    main()
