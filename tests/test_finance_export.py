from copy import deepcopy

import pytest

from billing import rate
from finance_export import encoded, invoice_rounding, prepare, publish


def fixture():
    cutoff = "2026-11-01T00:00:00+00:00"
    events = [
        {
            "tenant_id": "one",
            "source": "api",
            "id": "original",
            "units": 11,
            "occurred_at": "2026-10-20T00:00:00+00:00",
            "received_at": cutoff,
            "correction_of": None,
            "payload_sha256": "0" * 64,
        }
    ]
    prices = [
        {
            "tenant_id": "one",
            "id": "v1",
            "effective_from": "2026-01-01T00:00:00+00:00",
            "unit_price_microusd": 250,
            "recorded_at": cutoff,
        }
    ]
    doc = dict(
        rate(events, prices, tenant_id="one", month="2026-10", known_at=cutoff),
        id="s1",
        revision=1,
        created_at=cutoff,
        known_at=cutoff,
    )
    return (
        [{"account_id": "one", "customer_id": "customer_a"}],
        [doc],
        [{"account_id": "one", "source_cutoff_utc": cutoff, "events": events, "prices": prices}],
    )


def build(values=None, **kwargs):
    return prepare(
        *(values or fixture()),
        ["2026-10"],
        stream_id="synthetic-test",
        export_sequence=1,
        previous_bundle_sha256=None,
        **kwargs,
    )


@pytest.mark.parametrize(
    "micros,cents,residual",
    [
        (0, 0, 0),
        (4999, 0, 4999),
        (5000, 1, -5000),
        (10000, 1, 0),
        (34501750, 3450, 1750),
        (33003250, 3300, 3250),
    ],
)
def test_statement_rounding_has_explicit_signed_residual(micros, cents, residual):
    assert invoice_rounding(micros) == {"invoice_cents": cents, "rounding_residual_microusd": residual}


@pytest.mark.parametrize("value", [-1, True, 1.1, "10000"])
def test_money_requires_nonnegative_integer_micro_usd(value):
    with pytest.raises(ValueError):
        invoice_rounding(value)


def test_export_exact_replay_and_no_overwrite(tmp_path):
    content = build()
    assert publish(tmp_path / "one", content) == "published"
    assert publish(tmp_path / "one", build()) == "replayed"
    changed = dict(content, **{"accounts.json": encoded([])})
    with pytest.raises(ValueError):
        publish(tmp_path / "one", changed)
    assert (tmp_path / "one" / "accounts.json").read_bytes() == content["accounts.json"]


def test_failure_after_staging_leaves_no_partial_bundle(tmp_path):
    def interrupt(_):
        raise RuntimeError("fault")

    with pytest.raises(RuntimeError):
        publish(tmp_path / "new", build(), before_publish=interrupt)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "foreign", "currency", "amount", "source"])
def test_invalid_complete_scope_never_publishes(mutation):
    accounts, docs, sources = deepcopy(fixture())
    if mutation == "duplicate":
        docs.append(deepcopy(docs[0]))
    elif mutation == "missing":
        docs[0]["revision"] = 2
    elif mutation == "foreign":
        sources[0]["events"][0]["tenant_id"] = "two"
    elif mutation == "currency":
        docs[0]["currency"] = "GBP"
    elif mutation == "amount":
        docs[0]["amount_microusd"] += 1
    else:
        sources[0]["events"] = []
    with pytest.raises(ValueError):
        build((accounts, docs, sources))


def test_replay_bytes_do_not_depend_on_source_order():
    accounts, docs, sources = fixture()
    extra = dict(sources[0]["events"][0], id="second", units=1)
    sources[0]["events"].append(extra)
    core = rate(
        sources[0]["events"],
        sources[0]["prices"],
        tenant_id="one",
        month="2026-10",
        known_at=docs[0]["known_at"],
    )
    docs[0].update(core)
    before = build((accounts, docs, sources))
    sources[0]["events"].reverse()
    assert build((accounts, docs, sources)) == before


def test_zero_usage_published_statement_is_valid():
    accounts, docs, sources = fixture()
    sources[0]["events"] = []
    docs[0].update(
        rate([], sources[0]["prices"], tenant_id="one", month="2026-10", known_at=docs[0]["known_at"])
    )
    assert build((accounts, docs, sources))


def test_credentials_and_unexpected_source_columns_are_never_accepted():
    values = fixture()
    values[2][0]["events"][0]["token"] = "do-not-export"
    with pytest.raises(ValueError):
        build(values)


def test_corrupt_file_never_becomes_visible(tmp_path):
    content = build()
    content["statements.json"] = encoded([])
    with pytest.raises(ValueError, match="hash or completeness"):
        publish(tmp_path / "corrupt", content)
    assert not (tmp_path / "corrupt").exists()


def test_valid_different_bundle_cannot_replace_existing(tmp_path):
    first = build()
    publish(tmp_path / "immutable", first)
    values = fixture()
    values[0][0]["customer_id"] = "different-customer"
    with pytest.raises(FileExistsError):
        publish(tmp_path / "immutable", build(values))
    assert (tmp_path / "immutable" / "accounts.json").read_bytes() == first["accounts.json"]
