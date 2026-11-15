# A bounded database failure, then an exact retry

An admitted HTTP request can still fail while waiting for a database lock. Meterwork
now bounds that wait on every restricted connection, returns a protected 503 for
expected database availability failures, and keeps the write outcome explicitly
unknown. The server never invents a fresh event identity or automatically replays a
database operation. The SDK's bounded event-delivery retries reuse the original ID.

## Reproduced problems

On the pre-fix source `ae0fb03000cee395047f1a90e7186950dd6a6b9f`, an actual local
Uvicorn/PostgreSQL 17.11 request with `correction_of: ""` returned HTTP 500. The
request model accepted it, Python treated it as no correction, and PostgreSQL
rejected the inserted empty foreign key. The model and storage boundary now
require either null or a nonempty event identifier and return a client error
without opening the storage transaction for that input.

Fresh restricted-runtime connections also reported both `statement_timeout=0`
and `lock_timeout=0`. Several methods set their own limits, but sandbox provisioning,
health and startup inspection bypassed those per-method settings. Connection startup
now establishes a five-second statement timeout and two-second lock timeout,
including when a supplied connection string requests different `options`.

Temporary database failures after authentication/admission formerly escaped as
server errors. Those expected failures now return 503, `Retry-After: 2`, no-store
and the same security headers as successful requests. SQL/connection exception
details are not exposed. Programming faults are still errors rather than being
mislabelled as temporary unavailability.

## Observed local result: 15 November 2026

The [immutable report](request-recovery-2026-11-15.json) records **17 passing checks
over 14 real loopback HTTP requests**, using one Uvicorn process and an isolated
PostgreSQL 17.11 cluster on Windows. Its file hashes identify the measured source;
`source_commit: local-uncommitted` is not a hosted-run claim.

| Boundary | Observed result |
|---|---|
| Malformed correction | HTTP 422; zero ledger rows written |
| Contended account write and statement publication | Both actual lock waits independently observed through `pg_stat_activity`/`pg_blocking_pids`; protected 503s in 2.20 s and 2.09 s |
| Account separation during contention | A second account successfully writes while the first account's lock remains held |
| Previous valid output | The original 11-unit event and 2,750-microUSD statement remain unchanged while the blocker still holds its lock |
| Retry after release | The same 7-unit event ID is accepted once; its second delivery is a duplicate; revision 2 reconciles to `(11+7) × 250 = 4,500 microUSD` |
| Sandbox provisioning contention | Observed lock wait returns a protected 503 in 2.07 s, creates no account, and succeeds after the blocker releases |

The timeout observations include HTTP and connection overhead; they are not exact
response-time guarantees. The source data and faults are synthetic. All other
responses in this bounded exercise were 200 or 201; no 500 response occurred after
the fix. This is not a denial-of-service test or maximum-throughput benchmark.

Local regression also passed 166 Python tests and 10 JavaScript tests, followed by
the existing workload, HTTP, privilege, fresh-database restore, multiworker and
finance-export exercises. The original public snapshot still reproduces exactly.
`.gitattributes` now keeps source text in LF form and preserves JSON evidence bytes
so a default Windows checkout does not invalidate these fingerprints.

## Reproduce

Use an **isolated disposable PostgreSQL cluster** and a control database whose
name ends in `_test`, then supply its URL privately as `TEST_DATABASE_URL`:

```sh
python verify_request_recovery.py --output local-evidence/request-recovery-new.json
```

The harness creates and drops only its own generated database, but bootstrap roles
are cluster-global. Do not run it against a cluster hosting an active application.
The Uvicorn process receives only the restricted runtime DSN. Request records omit
tokens, account identifiers and connection strings. The independent observer holds
the lock first and waits for actual PostgreSQL waiters; a guessed delay never
establishes the fault boundary. Existing reports cannot be overwritten.

CI repeats this gate and retains `request-recovery-ci.json` with the existing
HTTP, runtime, multiworker, restore and finance evidence. Earlier published
workload/finance reports stay unchanged and are now checked against their original
source revision, rather than silently replacing their historical fingerprints.

## What a client may infer

These particular lock failures occurred before the writes, as verified independently.
A **general 503 does not establish rollback**: a connection can be lost after commit.
Retry usage with the same account, source, ID and payload; a changed payload must
still conflict. Statement publication keeps its existing content-idempotent behavior.
Sandbox creation has no request-idempotency contract: do not promise exactly-once
provisioning after an ambiguous response.

The budgets are per SQL statement/lock acquisition. They do not bound a whole
multi-statement HTTP operation, request upload time, transaction total time or
pre-admission authentication cost. The connection timeout remains ten seconds.
No hosted backend, production SLA, independent human review or customer adoption
is established by this exercise.

Primary references: [PostgreSQL statement and lock timeouts](https://www.postgresql.org/docs/17/runtime-config-client.html)
and [Psycopg connection parameters and transaction context](https://www.psycopg.org/psycopg3/docs/api/connections.html).
