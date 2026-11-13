# API reliability: from request to committed statement

Meterwork must acknowledge a retry without charging twice, reject an account crossing, and publish a complete statement while more usage arrives. A database method test does not cover JSON parsing, authentication, response policy or the actual ASGI server. `verify_http.py` exercises that boundary using real HTTP to Uvicorn and a dedicated PostgreSQL database.

The measurements below are an immutable earlier HTTP release. [The subsequent runtime upgrade](RUNTIME_SECURITY.md) removes the administrative credential from the HTTP process and replaces the shared allowance with separate account/anonymous admission. Do not read the earlier numbers as a measurement of the later source.

## Observed result — 13 November 2026

[CI run 43342144635](https://github.com/VasilenData/usage-metering-platform/actions/runs/43342144635) passed on source [`e99e3478`](https://github.com/VasilenData/usage-metering-platform/tree/e99e3478b82f9acc4d2b5a381c4e4935b16fe097). The downloaded `postgres-test-evidence` ZIP matched GitHub's SHA256 digest `a5f526a6d080d97b9e76937736df8168f3c3e1ad383caeee09cf343a40b67990`. The raw [HTTP result](http-2026-11-13.json) and [fresh recovery result](release-2026-11-13.json) are unchanged copies of that artifact, with source hashes verified against the implementation.

| Measurement | Observed result and scope |
|---|---|
| Environment | GitHub-hosted Linux; PostgreSQL 17.12; one Uvicorn process at a time; synthetic loopback requests. |
| Verification | 121 Python tests, no failures/errors/skips; 19 HTTP assertions including two pre-fix counterexamples; 15 Store-level recovery assertions. |
| Current API requests | 345 total: 74×200, 3×201, 244×401, 8×409, 2×413, 13×422, 1×429; **zero 5xx**. Rejections are intentional test outcomes. |
| Workload breakdown | 104 functional/concurrency requests with up to eight workers; separately, 241 sequential unauthenticated requests to test the unchanged limiter. Health/readiness polls are excluded. |
| Observed latency | Across the 104 functional/concurrency requests: median **91.460 ms**, p95 **152.632 ms**, maximum **160.302 ms**. This includes validation and rejection cases; it is not an accepted-ingestion-only or capacity benchmark. |
| Billing race | Seven immutable revisions all reconciled independently; final 194 units / 48,500 micro-USD matched fixture arithmetic. The winning conflicting payload and number of intermediate versions can differ in another valid schedule. |
| Before/after failure | Pinned pre-fix code returned 500 for NaN, overflow and NUL properties. Current source returned 422 without product writes. The three intentional historical 500s are separate from current-request totals. |
| Recovery, separate path | Abrupt publisher termination/retry preserved prior output; a fresh backup/restore reconciled all 11 fixture tenants and retained RLS. These are Store-level checks, not HTTP disaster-recovery guarantees. |

This closes a concrete HTTP-boundary evidence gap. It does not establish actual users, sustained availability or independent human approval. The [limitations below](#what-this-does-not-establish) still apply.

## Counterexamples and changes

Two request-boundary weaknesses motivated this upgrade:

- Early 400/413/429 middleware responses bypassed cache/security headers. The local pre-fix oversized-body response returned 413 with neither `Cache-Control` nor CSP. All middleware-generated responses now pass through the same header function.
- Product properties accepted values the database cannot store: `NaN`, exponent overflow (`1e400`) and escaped NUL. CI runs these requests against immutable commit `94a4cb1885297442ef59bdcf9fcecc605ff2a97b` and requires actual HTTP 500 responses before accepting the counterexample. It then requires safe 422 responses and unchanged product rows on current source. Additional regressions cover infinities, invalid Unicode, duplicate keys, malformed syntax and deep nesting.

The bounded parser validates bytes before authentication and SQL access. Duplicate keys are rejected rather than letting different parsers choose different values. Input error responses omit the submitted body. Normal Unicode and nested JSON remain supported. PostgreSQL's [JSON documentation](https://www.postgresql.org/docs/17/datatype-json.html) explains the NUL/Unicode restrictions; Python's [JSON documentation](https://docs.python.org/3/library/json.html) documents permissive number decoding and recommends bounding untrusted input.

## Executable acceptance criteria

| Scenario | Required invariant |
|---|---|
| 24 identical requests, eight clients | Exactly one accepted usage event and 23 duplicate acknowledgements. |
| 16 concurrent requests with two conflicting payloads | One immutable winner, seven equivalent replays and eight 409 conflicts. |
| Eight corrections competing for 100 original units | Five −20 corrections accepted, three rejected; balance never below zero. |
| Eight simultaneous first assignments | One stable event/variant for the actor. |
| Eight unchanged statement publications | One statement identity. |
| Ingestion racing with publication | Continuous revisions; prior statement unchanged; every version reconciles at its own database cutoff. |
| Same event ID in two accounts | Separate ledgers; foreign correction and body tenant selector rejected; token scope wins over a query selector. |
| Missing, altered, invalid and expired credentials | 401; no statement disclosure. Expiry is controlled only by fixture administration. |
| Unsupported JSON and oversized bodies | 422 or 413; no product write; security/cache headers present, including chunked requests. |
| Fresh process, 241 sequential unauthenticated requests | 240 ordinary 401 responses followed by one 429 and `Retry-After: 60`; current source applies this to the anonymous peer allowance. |

The SQL oracle does not call the Python rating engine. It independently selects effective prices known at each statement cutoff and reconciles event counts, units and micro-US-dollar amounts. Fixed fixture arithmetic supplies an additional final-total check. Concurrent schedules may produce different numbers of intermediate valid revisions; the harness checks invariants, not a preferred interleaving.

## Reproduce and inspect the result

Install `requirements-dev.txt`, provision a dedicated disposable PostgreSQL database ending in `_test`, and set `TEST_DATABASE_URL` in the environment. Then run:

```sh
python verify_http.py --output local-evidence/http.json
```

The command binds Uvicorn only to `127.0.0.1`, disables access logs and proxy-header rewriting, gives the current app only the restricted runtime DSN, keeps tokens in memory, terminates its application children and removes only accounts it created. PostgreSQL itself remains under the caller's control. Optional `--baseline-dir /path/to/pinned-checkout` reproduces the old errors; CI supplies the pinned checkout automatically. The baseline must have the exact pinned HEAD and tracked file bytes, with no additional Python sources; the report records its verified hashes. A different or modified checkout fails instead of being mislabelled. Do not use a shared or production database. No outbound notification is sent.

On [GitHub Actions](https://github.com/VasilenData/usage-metering-platform/actions), open a successful **Ledger and product evidence** run and download **postgres-test-evidence**:

- `http-ci.json`: exact host/commit/run, eight-client scope, per-request scenario/status/elapsed time, check results, HTTP status totals, latency distribution and source hashes. It excludes tokens, request bodies and database connection strings.
- `release-ci.json`: a fresh Store-level publisher interruption, recovery and full database backup/restore exercise, explicitly separate from the HTTP measurements.
- `workload-ci.json`: fresh small multi-account reconciliation.
- `runtime-ci.json`: the subsequent restricted-role, token-derived row security and account/anonymous fairness exercise. Its own source hashes identify the tested implementation.
- `tests.xml`: the full Python test result.

Use the artifact from the source revision being reviewed. The November 12 report is historical evidence, verified against its original commit; its numbers are not updated to imply a new run. A GitHub-hosted Linux service-container exercise is different from the earlier local Windows run. Timing includes HTTP, token lookup, connections and locking; it is neither a public-network latency measurement nor maximum sustainable throughput. The sequential limiter probe is excluded from the main latency distribution and identified separately.

## What this does not establish

This is an AI-assisted implementation and review, not an independent human security assessment. It does not prove the owner's unaided coding ability, real customer adoption or sustained production operation. The public site remains a read-only synthetic snapshot; no live API was deployed by this upgrade.

One app process and one PostgreSQL node are exercised. There is no TLS termination, public gateway, distributed limiter, multi-region failure, database power-loss test or external penetration test. In this earlier measured release, clients shared a limiter and the HTTP host retained an administrative credential. [The subsequent runtime upgrade](RUNTIME_SECURITY.md) addresses those boundaries with a separate runtime credential, fixed control functions, bearer-derived row security and distinct request allowances. Shared compute/database resources, bearer theft, network controls and distributed enforcement remain limits. No comprehensive security certification is claimed.

For a genuine external review, ask an engineer to examine `identity`, `protections`, `RuntimeStore.tenant`, `runtime_schema.sql`, `Store.ingest_usage` and `Store.publish`, and run the artifact reproduction independently. Ask them to challenge one invariant, propose a counterexample and record a review comment or issue. Preserve the response and subsequent change only after the review happens. For owner practice, explain why a competing correction cannot overspend the original, then modify a new edge case and diagnose its failure without reference answers. Neither activity is represented as completed here.
