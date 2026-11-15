# From usage events to a reconciled finance balance

**Problem:** retried usage, a late correction and sub-cent prices must not silently become duplicate invoices, rewritten history or unexplained revenue. Meterwork now supplies an immutable, versioned source contract to the separate Finance Mart USD integration.

[View the read-only proof](https://vasilendata.github.io/usage-metering-platform/finance-bridge/) · [Inspect the exporter](../finance_export.py) · [Actual HTTP/PostgreSQL evidence](../web/finance-bridge/report.json)

## Observed local result — 15 November 2026

The restricted PostgreSQL 17.11 runtime and a real loopback Uvicorn process passed **14 checks across 14 HTTP requests**. Two sandbox accounts were created through the HTTP API. Their usage and statement revisions were written through that same API; the exporter read the actual ledger using each account's bearer-derived scope. No administrative query supplied the exported events or statements.

| Account / revision | Units | Exact statement, microUSD | Invoice policy, USD cents | Residual, microUSD |
|---|---:|---:|---:|---:|
| Customer A, original | 150,008 | 34,501,750 | 3,450 | 1,750 |
| Customer A, corrected | 144,014 | 33,003,250 | 3,300 | 3,250 |
| Customer B, unchanged | 99,999 | 19,999,800 | 2,000 | -200 |

The independent hand calculation is `90,003*250 + 60,005*200` for A's original statement. A correction of `-10,001*250` and late usage of `4,007*250` change it by **-1,498,500 microUSD**. The invoice change is **-150 cents**, and the carried rounding residual changes by **+1,500 microUSD**. Both sides reconcile exactly: `-150*10,000 + 1,500 = -1,498,500`.

An HTTP retry of the original event was identified as a duplicate delivery, and repeated statement publication returned the exact existing statement. Re-exporting produced identical bytes for all four files. A second bundle retained A1 and B1 unchanged and added A2. A token for A could not export B's ledger. An injected exception after complete staging but before rename left the first published directory intact and no partially published destination.

This is **synthetic local evidence**, not live billing, collected money, customer adoption, sustained operation or external review. Meterwork statements remain non-binding estimates. The invoice policy and Finance's payment/refund inputs are an explicit demonstration scenario.

## Contract `meterwork-finance/v1`

Each immutable directory contains:

| File | Grain and purpose |
|---|---|
| `accounts.json` | One account UUID → one stable customer alias. No tokens or token hashes. |
| `statements.json` | One exact API statement per account/month/revision, cumulatively `1..N`. Statement IDs and fingerprints are preserved. |
| `source_snapshots.json` | One scoped usage/price history per account, with its own declared knowledge cutoff. |
| `manifest.json` | Contract, units, rounding, stream/sequence/predecessor, complete account-month revision counts and file hashes. |

Every declared account × month requires a published statement, including a zero-usage statement. Event identity is `(account, source, event_id)`; corrections refer to the original event within that scope. Statement identity is `(account, statement_id)`; revision ordering is `(account, month, revision)`. Finance must select the latest revision for a current invoice and retain prior versions as history, never sum all historical revisions as current revenue.

The separate USD domain retains integer **microUSD** (1 USD = 1,000,000 microUSD). It does not convert, combine or relabel Finance Mart's original GBP pence. The scenario invoice policy rounds **once at the nonnegative statement total**, half-up:

```text
invoice_cents = (amount_microusd + 5000) // 10000
residual_microusd = amount_microusd - invoice_cents * 10000
amount_microusd = invoice_cents * 10000 + residual_microusd
```

The residual is explicit and lies between -5,000 and 4,999 microUSD. It is not silently discarded, collected or treated as additional revenue.

`occurred_at` chooses the billing month and effective price; `received_at` and price `recorded_at` determine what was known. Each statement reproduces at its own `known_at`. Each account snapshot includes source rows received/recorded up to that account's maximum selected statement cutoff. The manifest's maximum cutoff is an audit label, **not a cross-account consistent-transaction guarantee**. The exercise pauses writes while capturing both accounts. This is a bounded statement export, not a global usage feed or a claim to include unbilled events beyond the selected cutoffs.

Payloads are sorted canonical UTF-8 JSON with one LF. File SHA-256 values cover those exact bytes. `bundle_sha256` hashes canonical manifest JSON excluding itself, without a trailing LF. Hashes detect content changes; they are not signatures or proof of a trusted producer. The receiver also independently rerates supplied source histories. A malicious trusted producer rewriting both the source and its manifest is outside this contract's trust boundary.

The stream starts at sequence 1 with a null predecessor. A new delivery advances exactly one sequence and names the previous bundle hash. Exact whole-bundle replay is idempotent; duplicate identities *inside* a bundle are invalid. Missing delivery, changed historical content, wrong hash/count, incomplete revisions or inconsistent monetary amounts must be visible exceptions at the receiver, not repaired with invented revenue.

## Reproduce

Use a dedicated disposable PostgreSQL cluster and a database name ending `_test`. The exercise creates and drops only generated databases and reconfigures demonstration roles in that cluster:

```sh
pip install -r requirements-dev.txt
python verify_finance_export.py --output local-evidence/finance-export-ci
pytest -q
```

Set `TEST_DATABASE_URL` privately for the exercise. The actual HTTP process receives only the restricted runtime DSN. The exporter can also run separately with `METER_RUNTIME_DATABASE_URL` and a private bindings file containing `{account_id, customer_id, token}` rows:

```sh
python finance_export.py --bindings /private/bindings.json --month 2026-10 \
  --stream-id my-synthetic-stream --sequence 1 --output /private/export-001
python finance_export.py --bindings /private/bindings.json --month 2026-10 \
  --stream-id my-synthetic-stream --sequence 2 \
  --previous-manifest /private/export-001/manifest.json --output /private/export-002
```

Never commit the bindings file or connection details. Only explicitly synthetic account data is appropriate for the public example. Exporting a real account's ledger requires separate authorization and privacy controls.

Publication validates the whole set in memory, writes and flushes a temporary sibling directory, then renames it into place. Existing identical output is an idempotent replay; different existing output is rejected. This demonstrates same-filesystem atomic visibility for one exporter, not power-loss durability, multi-writer coordination, an external broker acknowledgement or a cross-repository transaction. The receiver separately validates before publishing its own version.

The code and review are AI-assisted. This integration demonstrates reproducible engineering artifacts; it does not establish unaided interview skill or independent human review.
