"""Export only the synthetic fixture account; independently verify the public bundle."""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from analytics import build_report
from billing import canonical, rate, timestamp
from demo import overview
from store import Store


def verify(directory):
    directory = Path(directory)
    data = json.loads((directory / "demo.json").read_text(encoding="utf-8"))
    source = json.loads((directory / "sources.json").read_text(encoding="utf-8"))
    assert data["scope"] == source["scope"] == "synthetic-demo"
    assert data["tenant"]["id"] == source["tenant_id"] == "demo"
    assert data["generated_at"] == source["as_of"]
    assert (
        hashlib.sha256((directory / "sources.json").read_bytes()).hexdigest() == data["source_bundle_sha256"]
    )
    assert all(
        row["tenant_id"] == "demo"
        for key in ("usage_events", "prices", "product_events")
        for row in source[key]
    )
    assert all(
        row["actor_id"].startswith("synthetic-") and row["properties"] == {"synthetic": True}
        for row in source["product_events"]
    )
    regenerated = build_report(source["product_events"], tenant_id="demo", as_of=source["as_of"])
    assert regenerated == data["analytics"], "Product snapshot does not reproduce"
    for document in data["statements"]:
        cutoff = timestamp(document["created_at"])
        known = [row for row in source["usage_events"] if timestamp(row["received_at"]) <= cutoff]
        expected = rate(known, source["prices"], tenant_id="demo", month=document["month"])
        actual = {
            key: value for key, value in document.items() if key not in ("id", "revision", "created_at")
        }
        assert expected == actual, f"Statement revision {document['revision']} does not reproduce"
    assert data["metering"]["total_units"] == sum(row["units"] for row in source["usage_events"])
    assert data["metering"]["accepted_events"] == len(source["usage_events"])
    assert data["metering"]["estimated_amount_microusd"] == sum(
        rate(source["usage_events"], source["prices"], tenant_id="demo", month=month)["amount_microusd"]
        for month in {timestamp(row["occurred_at"]).strftime("%Y-%m") for row in source["usage_events"]}
    )
    return {
        "statements_reproduced": len(data["statements"]),
        "product_events": len(source["product_events"]),
        "usage_events": len(source["usage_events"]),
        "source_bundle_sha256": data["source_bundle_sha256"],
    }


def export(directory):
    store = Store(os.environ["DATABASE_URL"])
    data = overview(store, "demo")
    events, prices, product, _ = store.snapshot("demo")
    source = {
        "scope": "synthetic-demo",
        "tenant_id": "demo",
        "as_of": data["generated_at"],
        "usage_events": events,
        "prices": prices,
        "product_events": product,
    }
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meterwork-snapshot-") as temporary:
        stage = Path(temporary)
        source_bytes = (canonical(source) + "\n").encode()
        (stage / "sources.json").write_bytes(source_bytes)
        data["source_bundle_sha256"] = hashlib.sha256(source_bytes).hexdigest()
        (stage / "demo.json").write_text(canonical(data) + "\n", encoding="utf-8")
        evidence = verify(stage)
        for name in ("sources.json", "demo.json"):
            (directory / name).write_bytes((stage / name).read_bytes())
    # Public publication is one GitHub Pages artifact, after the complete bundle
    # passes this verifier in CI; these local writes are not an atomic deployment.
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--directory", default="web")
    args = parser.parse_args()
    print(json.dumps(verify(args.directory) if args.verify else export(args.directory), sort_keys=True))


if __name__ == "__main__":
    main()
