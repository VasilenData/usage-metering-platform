"""Immutable, scoped usage-to-finance bundles. This is not a live CDC feed."""

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from billing import canonical, rate, timestamp
from runtime_store import RuntimeStore

CONTRACT = "meterwork-finance/v1"
FILES = ("accounts.json", "statements.json", "source_snapshots.json")
EVENT_FIELDS = {
    "tenant_id",
    "source",
    "id",
    "occurred_at",
    "received_at",
    "units",
    "correction_of",
    "payload_sha256",
}
PRICE_FIELDS = {"tenant_id", "id", "effective_from", "unit_price_microusd", "recorded_at"}
STATEMENT_METADATA = {"id", "revision", "created_at", "known_at"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (canonical(value) + "\n").encode("utf-8")


def plain(value):
    return json.loads(canonical(value))


def invoice_rounding(amount_microusd):
    """One nonnegative statement estimate, half-up at the final invoice total."""
    if type(amount_microusd) is not int or amount_microusd < 0:
        raise ValueError("Statement amount must be nonnegative integer microUSD")
    cents = (amount_microusd + 5000) // 10000
    return {"invoice_cents": cents, "rounding_residual_microusd": amount_microusd - cents * 10000}


def validate_payload(accounts, statements, sources, months):
    """Validate completeness inside explicitly requested account/month scope."""
    if not accounts or len(accounts) > 64 or not months or len(set(months)) != len(months):
        raise ValueError("Require 1..64 accounts and unique requested months")
    if any(not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month) for month in months):
        raise ValueError("Invalid requested month")
    account_ids = [row["account_id"] for row in accounts]
    customer_ids = [row["customer_id"] for row in accounts]
    if any(set(row) != {"account_id", "customer_id"} for row in accounts):
        raise ValueError("Account export contains unexpected fields")
    if any(not isinstance(value, str) or not value for value in account_ids + customer_ids):
        raise ValueError("Account and customer identities must be nonempty strings")
    if len(set(account_ids)) != len(accounts) or len(set(customer_ids)) != len(accounts):
        raise ValueError("Duplicate account or customer identity")
    if len(sources) != len(accounts) or {row["account_id"] for row in sources} != set(account_ids):
        raise ValueError("Source scope differs from account mapping")
    source_map = {row["account_id"]: row for row in sources}
    identities = set()
    grouped = {(account, month): [] for account in account_ids for month in months}
    for doc in statements:
        pair = (doc["tenant_id"], doc["month"])
        if pair not in grouped or doc.get("currency") != "USD" or doc.get("status") != "estimate":
            raise ValueError("Statement outside USD estimate scope")
        identity = (doc["tenant_id"], doc["id"])
        if identity in identities or type(doc["revision"]) is not int:
            raise ValueError("Duplicate statement identity or invalid revision")
        identities.add(identity)
        grouped[pair].append(doc)
    expected = []
    for (account, month), docs in sorted(grouped.items()):
        docs.sort(key=lambda row: row["revision"])
        if [row["revision"] for row in docs] != list(range(1, len(docs) + 1)) or not docs:
            raise ValueError("Missing or duplicate statement revision within declared scope")
        if any(timestamp(b["known_at"]) <= timestamp(a["known_at"]) for a, b in zip(docs, docs[1:])):
            raise ValueError("Revision knowledge cutoffs must increase")
        expected.append({"account_id": account, "month": month, "revision_count": len(docs)})
    for account, source in source_map.items():
        if set(source) != {"account_id", "source_cutoff_utc", "events", "prices"}:
            raise ValueError("Source export contains unexpected fields")
        docs = [row for row in statements if row["tenant_id"] == account]
        cutoff = max(timestamp(row["known_at"]) for row in docs)
        if timestamp(source["source_cutoff_utc"]) != cutoff:
            raise ValueError("Source cutoff differs from selected statements")
        for collection, fields, clock, key in (
            (source["events"], EVENT_FIELDS, "received_at", lambda row: (row["source"], row["id"])),
            (source["prices"], PRICE_FIELDS, "recorded_at", lambda row: row["id"]),
        ):
            seen = set()
            for row in collection:
                if set(row) != fields or row["tenant_id"] != account or timestamp(row[clock]) > cutoff:
                    raise ValueError("Invalid scoped source row or receipt cutoff")
                identity = key(row)
                if identity in seen:
                    raise ValueError("Duplicate source identity")
                seen.add(identity)
        for doc in docs:
            calculated = rate(
                source["events"],
                source["prices"],
                tenant_id=account,
                month=doc["month"],
                known_at=doc["known_at"],
                engine_version=doc["engine_version"],
            )
            if plain(calculated) != {k: v for k, v in doc.items() if k not in STATEMENT_METADATA}:
                raise ValueError("Statement does not reproduce from its exact source and knowledge cutoff")
            invoice_rounding(doc["amount_microusd"])
    return expected


def capture(runtime, bindings, months):
    """Credentials stay in memory; each account snapshot uses its bearer/RLS scope."""
    accounts, statements, sources = [], [], []
    for binding in sorted(bindings, key=lambda row: row["account_id"]):
        account = binding["account_id"]
        events, prices, _, documents = runtime.scoped(binding["token"]).snapshot(account)
        selected = sorted(
            (plain(doc) for doc in documents if doc["month"] in months),
            key=lambda row: (row["month"], row["revision"]),
        )
        if not selected:
            raise ValueError("Every declared account requires a published statement")
        cutoff = max(timestamp(row["known_at"]) for row in selected)
        accounts.append({"account_id": account, "customer_id": binding["customer_id"]})
        statements.extend(selected)
        sources.append(
            {
                "account_id": account,
                "source_cutoff_utc": cutoff.isoformat(),
                "events": plain(
                    sorted(
                        (
                            {k: row[k] for k in EVENT_FIELDS}
                            for row in events
                            if timestamp(row["received_at"]) <= cutoff
                        ),
                        key=lambda row: (row["source"], row["id"]),
                    )
                ),
                "prices": plain(
                    sorted(
                        (
                            {k: row[k] for k in PRICE_FIELDS}
                            for row in prices
                            if timestamp(row["recorded_at"]) <= cutoff
                        ),
                        key=lambda row: (row["effective_from"], row["id"]),
                    )
                ),
            }
        )
    validate_payload(accounts, statements, sources, months)
    return accounts, statements, sources


def prepare(accounts, statements, sources, months, *, stream_id, export_sequence, previous_bundle_sha256):
    accounts, statements, sources = plain(accounts), plain(statements), plain(sources)
    accounts.sort(key=lambda row: row["account_id"])
    statements.sort(key=lambda row: (row["tenant_id"], row["month"], row["revision"]))
    sources.sort(key=lambda row: row["account_id"])
    for source in sources:
        source["events"].sort(key=lambda row: (row["source"], row["id"]))
        source["prices"].sort(key=lambda row: (row["effective_from"], row["id"]))
    expected = validate_payload(accounts, statements, sources, months)
    if not isinstance(stream_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", stream_id):
        raise ValueError("Invalid stream identity")
    if type(export_sequence) is not int or export_sequence < 1:
        raise ValueError("Export sequence must be a positive integer")
    if (export_sequence == 1 and previous_bundle_sha256 is not None) or (
        export_sequence > 1 and not re.fullmatch(r"[0-9a-f]{64}", previous_bundle_sha256 or "")
    ):
        raise ValueError("Predecessor hash does not match export sequence")
    content = dict(zip(FILES, (encoded(accounts), encoded(statements), encoded(sources))))
    manifest = {
        "contract_version": CONTRACT,
        "scope": "synthetic-local",
        "currency": "USD",
        "statement_money_unit": "microUSD",
        "invoice_money_unit": "USD_cent",
        "rounding": "statement_total_half_up",
        "stream_id": stream_id,
        "export_sequence": export_sequence,
        "previous_bundle_sha256": previous_bundle_sha256,
        "source_cutoff_utc": max(row["source_cutoff_utc"] for row in sources),
        "expected_account_months": expected,
        "files": {
            name: {"sha256": sha256(data), "rows": len(json.loads(data))} for name, data in content.items()
        },
    }
    manifest["bundle_sha256"] = sha256(canonical(manifest).encode("utf-8"))
    return {**content, "manifest.json": encoded(manifest)}


def validate_bundle(content):
    if set(content) != {*FILES, "manifest.json"}:
        raise ValueError("Incomplete publication file set")
    documents = {name: json.loads(data) for name, data in content.items()}
    if any(encoded(documents[name]) != data for name, data in content.items()):
        raise ValueError("Bundle is not canonically encoded")
    manifest = documents["manifest.json"]
    if manifest["bundle_sha256"] != sha256(
        canonical({k: v for k, v in manifest.items() if k != "bundle_sha256"}).encode("utf-8")
    ):
        raise ValueError("Manifest fingerprint mismatch")
    if set(manifest["files"]) != set(FILES):
        raise ValueError("Manifest file set is incomplete")
    for name in FILES:
        if manifest["files"][name] != {"sha256": sha256(content[name]), "rows": len(documents[name])}:
            raise ValueError("Source file hash or completeness count mismatch")
    months = sorted({row["month"] for row in manifest["expected_account_months"]})
    rebuilt = prepare(
        *(documents[name] for name in FILES),
        months,
        stream_id=manifest["stream_id"],
        export_sequence=manifest["export_sequence"],
        previous_bundle_sha256=manifest["previous_bundle_sha256"],
    )
    if rebuilt != content:
        raise ValueError("Manifest metadata or complete account/month scope differs")


def publish(destination, content, *, before_publish=None):
    """New immutable directory becomes visible only after its complete files exist.

    Existing destinations are never replaced. Directory rename is same-filesystem
    atomic visibility, not a claim of durability through power loss.
    """
    destination = Path(destination)
    validate_bundle(content)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_dir() or {p.name for p in destination.iterdir()} != set(content):
            raise FileExistsError("Existing bundle differs; immutable publication refused")
        if any((destination / name).read_bytes() != data for name, data in content.items()):
            raise FileExistsError("Existing bundle differs; immutable publication refused")
        return "replayed"
    with tempfile.TemporaryDirectory(prefix=".finance-stage-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir()
        for name, data in content.items():
            with (staged / name).open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        if before_publish:
            before_publish(staged)
        # os.rename refuses replacement of a nonempty directory on supported
        # platforms. A racing identical exporter retries through the guard above.
        staged.rename(destination)
    return "published"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bindings", required=True, help="Private JSON array: account_id, customer_id, token"
    )
    parser.add_argument("--month", action="append", required=True)
    parser.add_argument("--stream-id", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--previous-manifest")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    previous = (
        json.loads(Path(args.previous_manifest).read_text())["bundle_sha256"]
        if args.previous_manifest
        else None
    )
    result = prepare(
        *capture(
            RuntimeStore(os.environ["METER_RUNTIME_DATABASE_URL"]),
            json.loads(Path(args.bindings).read_text()),
            args.month,
        ),
        args.month,
        stream_id=args.stream_id,
        export_sequence=args.sequence,
        previous_bundle_sha256=previous,
    )
    outcome = publish(args.output, result)
    print(
        json.dumps(
            {"outcome": outcome, "bundle_sha256": json.loads(result["manifest.json"])["bundle_sha256"]}
        )
    )


if __name__ == "__main__":
    main()
