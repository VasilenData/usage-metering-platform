# API reliability: from request to committed statement

Meterwork must acknowledge a retry without charging twice, reject an account crossing, and publish a complete statement while more usage arrives. A database method test does not cover JSON parsing, authentication, response policy or the actual ASGI server. `verify_http.py` exercises that boundary using real HTTP to Uvicorn and a dedicated PostgreSQL database.

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
| Fresh process, 241 sequential unauthenticated requests | 240 ordinary 401 responses followed by one 429 and `Retry-After: 60`; limiter remains unchanged. |

The SQL oracle does not call the Python rating engine. It independently selects effective prices known at each statement cutoff and reconciles event counts, units and micro-US-dollar amounts. Fixed fixture arithmetic supplies an additional final-total check. Concurrent schedules may produce different numbers of intermediate valid revisions; the harness checks invariants, not a preferred interleaving.

## Reproduce and inspect the result

Install `requirements-dev.txt`, provision a dedicated disposable PostgreSQL database ending in `_test`, and set `TEST_DATABASE_URL` in the environment. Then run:

```sh
python verify_http.py --output local-evidence/http.json
```

The command binds Uvicorn only to `127.0.0.1`, disables access logs, keeps tokens in memory, terminates its application children and removes only accounts it created. PostgreSQL itself remains under the caller's control. Optional `--baseline-dir /path/to/pinned-checkout` reproduces the old errors; CI supplies the pinned checkout automatically. Do not use a shared or production database. No outbound notification is sent.

On [GitHub Actions](https://github.com/VasilenData/usage-metering-platform/actions), open a successful **Ledger and product evidence** run and download **postgres-test-evidence**:

- `http-ci.json`: exact host/commit/run, eight-client scope, per-request scenario/status/elapsed time, check results, HTTP status totals, latency distribution and source hashes. It excludes tokens, request bodies and database connection strings.
- `release-ci.json`: a fresh Store-level publisher interruption, recovery and full database backup/restore exercise, explicitly separate from the HTTP measurements.
- `workload-ci.json`: fresh small multi-account reconciliation.
- `tests.xml`: the full Python test result.

Use the artifact from the source revision being reviewed. The November 12 report is historical evidence, verified against its original commit; its numbers are not updated to imply a new run. A GitHub-hosted Linux service-container exercise is different from the earlier local Windows run. Timing includes HTTP, token lookup, connections and locking; it is neither a public-network latency measurement nor maximum sustainable throughput. The sequential limiter probe is excluded from the main latency distribution and identified separately.

## What this does not establish

This is an AI-assisted implementation and review, not an independent human security assessment. It does not prove the owner's unaided coding ability, real customer adoption or sustained production operation. The public site remains a read-only synthetic snapshot; no live API was deployed by this upgrade.

One app process and one PostgreSQL node are exercised. There is no TLS termination, public gateway, distributed limiter, multi-region failure, database power-loss test or external penetration test. A malicious client can consume the shared limiter budget. RLS protects against omitted SQL tenant predicates, while a compromised app host could still use its administrative environment credential. Production operation needs a separate provisioning control plane, rotated runtime credentials and appropriate network/abuse controls. No comprehensive security certification is claimed.

For a genuine external review, ask an engineer to examine `identity`, `protections`, `Store.tenant`, `Store.ingest_usage` and `Store.publish`, and run the artifact reproduction independently. Ask them to challenge one invariant, propose a counterexample and record a review comment or issue. Preserve the response and subsequent change only after the review happens. For owner practice, explain why a competing correction cannot overspend the original, then modify a new edge case and diagnose its failure without reference answers. Neither activity is represented as completed here.
