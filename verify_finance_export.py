"""Real restricted HTTP/ledger exports for a separate synthetic USD finance scenario."""

import argparse
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
from finance_export import capture, invoice_rounding, prepare, publish
from runtime_store import RuntimeStore
from statement_diff import compare_statements
from verify_http import usage
from verify_multiworker import worker
from verify_runtime_restore import fresh_database

ROOT = Path(__file__).resolve().parent


def run(admin_url, output):
    def source_hashes():
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted([*ROOT.glob("*.py"), *ROOT.glob("*.sql")])
        }

    source_identity = source_hashes()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    checks, requests = [], []
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "Synthetic local PostgreSQL and real loopback HTTP; no production billing or customer use",
        "source_commit": os.getenv("GITHUB_SHA", "local-uncommitted"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "checks": checks,
        "http_requests": requests,
    }

    def check(name, result, detail):
        checks.append({"name": name, "passed": bool(result), "detail": detail})
        if not result:
            raise AssertionError(name)

    def request(client, label, method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        requests.append({"scenario": label, "status": response.status_code})
        if response.status_code != expected:
            raise AssertionError(f"{label}: expected {expected}, received {response.status_code}")
        return response.json()

    def bundle(runtime, bindings, sequence, previous):
        return prepare(
            *capture(runtime, bindings, ["2026-10"]),
            ["2026-10"],
            stream_id="meterwork-finance-synthetic-v1",
            export_sequence=sequence,
            previous_bundle_sha256=previous,
        )

    with fresh_database(admin_url, closed=False) as url, tempfile.TemporaryDirectory() as temporary:
        password = secrets.token_urlsafe(32)
        bootstrap(url, password)
        runtime_url = make_conninfo(url, user="meter_api_runtime", password=password)
        runtime = RuntimeStore(runtime_url)
        with psycopg.connect(url) as conn:
            report["postgresql"] = conn.execute("SELECT version()").fetchone()[0]
        with worker(runtime_url, Path(temporary) / "unused-marker") as (_, client):
            a = request(client, "create customer_a sandbox", "POST", "/api/sandbox", expected=201)
            b = request(client, "create customer_b sandbox", "POST", "/api/sandbox", expected=201)
            bindings = [
                {"account_id": account["tenant_id"], "customer_id": customer, "token": account["token"]}
                for customer, account in (("customer_a", a), ("customer_b", b))
            ]
            headers_a = {"Authorization": "Bearer " + a["token"]}
            headers_b = {"Authorization": "Bearer " + b["token"]}
            original = usage("a-original", 90003)
            request(client, "A original usage", "POST", "/api/usage", headers=headers_a, json=original)
            request(
                client,
                "A new-price usage",
                "POST",
                "/api/usage",
                headers=headers_a,
                json=usage("a-after-price", 60005, occurred_at="2026-10-20T12:00:00Z"),
            )
            request(
                client,
                "B usage",
                "POST",
                "/api/usage",
                headers=headers_b,
                json=usage("b-original", 99999, occurred_at="2026-10-20T12:00:00Z"),
            )
            first_a = request(
                client,
                "A initial statement",
                "POST",
                "/api/statements",
                headers=headers_a,
                json={"month": "2026-10"},
            )
            first_b = request(
                client,
                "B initial statement",
                "POST",
                "/api/statements",
                headers=headers_b,
                json={"month": "2026-10"},
            )
            check(
                "A baseline hand calculation",
                (first_a["total_units"], first_a["amount_microusd"]) == (150008, 34501750),
                "90,003*250 + 60,005*200 = 34,501,750 microUSD",
            )
            check(
                "B baseline hand calculation",
                first_b["amount_microusd"] == 19999800,
                "99,999*200 = 19,999,800 microUSD",
            )
            first = bundle(runtime, bindings, 1, None)
            publish(output / "bundle-001", first)
            initial_hash = json.loads(first["manifest.json"])["bundle_sha256"]
            retry = request(
                client, "duplicate usage delivery", "POST", "/api/usage", headers=headers_a, json=original
            )
            retried_statement = request(
                client,
                "repeat statement publication",
                "POST",
                "/api/statements",
                headers=headers_a,
                json={"month": "2026-10"},
            )
            check(
                "HTTP usage retry is idempotent",
                retry["accepted"] is False,
                "Same event identity and payload",
            )
            check(
                "Statement retry is immutable",
                retried_statement == first_a,
                "Same statement ID, revision, knowledge cutoff and amount",
            )
            check(
                "Bundle retry is byte identical",
                bundle(runtime, bindings, 1, None) == first,
                "All four file bytes identical after an HTTP event and statement retry",
            )
            check(
                "Immutable publication accepts exact replay",
                publish(output / "bundle-001", first) == "replayed",
                "No replacement or new revision",
            )
            request(
                client,
                "A late correction",
                "POST",
                "/api/usage",
                headers=headers_a,
                json=usage("a-correction", -10001, correction_of="a-original"),
            )
            request(
                client,
                "A late original",
                "POST",
                "/api/usage",
                headers=headers_a,
                json=usage("a-late", 4007, occurred_at="2026-10-07T12:00:00Z"),
            )
            second_a = request(
                client,
                "A corrected statement",
                "POST",
                "/api/statements",
                headers=headers_a,
                json={"month": "2026-10"},
            )
            check(
                "A correction hand calculation",
                (second_a["total_units"], second_a["amount_microusd"]) == (144014, 33003250),
                "34,501,750 - 10,001*250 + 4,007*250 = 33,003,250 microUSD",
            )
            second = bundle(runtime, bindings, 2, initial_hash)
            publish(output / "bundle-002", second)
            docs = json.loads(second["statements.json"])
            observed_api = []
            for customer, headers in (("A", headers_a), ("B", headers_b)):
                result = request(
                    client,
                    customer + " immutable statement history",
                    "GET",
                    "/api/statements?month=2026-10",
                    headers=headers,
                )
                observed_api.extend(result)
            check(
                "Export is exact API statement history",
                sorted(docs, key=lambda row: row["id"]) == sorted(observed_api, key=lambda row: row["id"]),
                "All three revisions, no derived substitute",
            )
            check(
                "Prior revision bytes remain unchanged",
                first_a in docs and first_b in docs,
                "Bundle 002 contains A1, A2 and B1 cumulatively",
            )
            delta = compare_statements(first_a, second_a)
            check(
                "Correction delta is fully explained",
                delta["delta_amount_microusd"] == -1498500
                and delta["summary"]["usage_correction"]["delta_amount_microusd"] == -2500250
                and delta["summary"]["late_usage"]["delta_amount_microusd"] == 1001750,
                "Correction -2,500,250 plus late usage 1,001,750 = -1,498,500 microUSD",
            )
            before_round, after_round, b_round = [
                invoice_rounding(doc["amount_microusd"]) for doc in (first_a, second_a, first_b)
            ]
            check(
                "Rounded invoice change preserves remainder",
                before_round == {"invoice_cents": 3450, "rounding_residual_microusd": 1750}
                and after_round == {"invoice_cents": 3300, "rounding_residual_microusd": 3250}
                and b_round == {"invoice_cents": 2000, "rounding_residual_microusd": -200},
                "-150 cents*10,000 + 1,500 microUSD = -1,498,500 microUSD",
            )
            scoped = runtime.scoped(a["token"])
            denied = False
            try:
                scoped.snapshot(b["tenant_id"])
            except ValueError as error:
                denied = str(error) == "Account scope is unavailable or expired"
            check(
                "Wrong bearer/account export is denied",
                denied,
                "Read through restricted runtime and tenant guard",
            )

            def interruption(_):
                raise RuntimeError("Controlled interruption after staging, before publication")

            interrupted = False
            try:
                publish(output / "not-published", second, before_publish=interruption)
            except RuntimeError:
                interrupted = True
            check(
                "Interrupted export preserves prior published bundle",
                interrupted
                and not (output / "not-published").exists()
                and all((output / "bundle-001" / name).read_bytes() == data for name, data in first.items()),
                "Injected exception at complete staging boundary; no partial directory visible",
            )
            report["bundles"] = {
                name: json.loads(content["manifest.json"])
                for name, content in (("bundle-001", first), ("bundle-002", second))
            }
            report["correction_bridge"] = delta
            report["rounding"] = {
                "customer_a_revision_1": before_round,
                "customer_a_revision_2": after_round,
                "customer_b_revision_1": b_round,
            }
    check(
        "Source bytes unchanged during exercise",
        source_identity == source_hashes(),
        "All top-level Python and SQL sources fingerprinted before and after the run",
    )
    report["source_files"] = source_identity
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="local-evidence/finance-export-ci")
    args = parser.parse_args()
    report = run(os.environ["TEST_DATABASE_URL"], args.output)
    print(
        json.dumps(
            {
                "checks": len(report["checks"]),
                "passed": all(row["passed"] for row in report["checks"]),
                "http_requests": len(report["http_requests"]),
            }
        )
    )


if __name__ == "__main__":
    main()
