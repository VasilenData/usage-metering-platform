# Shared admission and an ambiguous HTTP commit

The original per-process limiter allowed one account to multiply its allowance by using more workers. A worker could also commit usage and disappear before its response reached the caller. These are different boundaries: admission must coordinate across workers, while retry correctness must follow the committed event identity.

## Implementation

`meter_api.admit_request` derives account identity from the unexpired bearer and maintains a rolling PostgreSQL window. It admits 120 requests per account or 240 per anonymous socket peer in 60 seconds, shared across every worker using that database. There are at most 256 active identities per kind; active windows are never evicted. Each identity stores only admitted timestamps, with a maximum of 120/240. A SHA256 digest is stored instead of a raw peer address. Expired identities are reclaimed on subsequent admission.

The application gets only EXECUTE on the reviewed function. It cannot read, insert, reset or delete the private window table. Startup rejects effective table **or column** grants, unexpected executable functions, unsafe role flags and the existing ledger-boundary drift. The function owner is `meter_control` (NOLOGIN, nonsuperuser), its search path is fixed to `pg_catalog`, and PUBLIC cannot execute it. Account identity is re-derived inside PostgreSQL; a caller-supplied account UUID is not trusted.

One short advisory transaction lock per kind serializes cleanup, capacity decisions and timestamp updates. Anonymous and account admission use separate locks. This is intentionally simple and bounded, with a measurable throughput trade-off; it is not a highly available gateway. Time is sampled after lock acquisition so waiting requests do not use a stale transaction timestamp. An admission lock wait longer than two seconds fails closed. Expected operational/time-out failures at authentication or admission return 503, `Retry-After: 2` and the same security/cache headers; the endpoint does not run. Unexpected programming errors remain failures.

The prior same-event identity rule handles an ambiguous response: after a confirmed commit and killed worker, the caller retries the **same** event ID, source and payload. The survivor returns `duplicate: true`; it does not append or charge a second event. This does not promise that a failed request was uncommitted.

## Acceptance evidence

The [immutable local report](multiworker-2026-11-14.json) records **32 passing acceptance checks**, Windows 11, Python 3.12.14 and PostgreSQL 17.11. It identifies the exact measured source files by SHA256; `source_commit` is explicitly `local-uncommitted` and there is no hosted-run claim. This is a shared workstation observation, not a dedicated benchmark host.

| Observed boundary | Result |
|---|---|
| Original two-worker counterexample | 240 successes +2 rejections for one account in 24.14 seconds; another process restart accepted again. |
| Shared account quota | 120 successes +40 rejections across both workers in 2.44 seconds. Another account still succeeded. |
| Shared anonymous quota | 240×401 +20×429 in 1.45 seconds, despite rotating forwarded headers. |
| Admission lock contention | Protected 503 within the bounded lock timeout; no ledger row written. |
| Observed-commit worker kill | No success response reached the client; survivor retry was a duplicate. Seven events, 77 units and 19,250 microUSD remained exactly once. |
| Bounded 30-second load | **350/350 HTTP 200**, split 175/175 between workers; observed 11.61 completed requests/second over 30.14 seconds. This is the declared finite offered workload, not maximum capacity. |
| Request latency | p50 **84.65 ms**, p95 **199.42 ms**, p99 **294.63 ms**; includes local HTTP, authentication, admission and connection setup. |
| Sampled database pressure | 481 samples; peak 2 runtime connections, peak 2 active, no sampled lock waiters; no observed database deadlocks or temp bytes in the load interval. Short peaks can be missed. |
| Regression gates | **132 Python and 10 JavaScript tests passed**; current HTTP 345 requests/19 checks, runtime 363 requests/27 checks, restored runtime 21 requests/23 checks, Store recovery 15 checks. |

Per-phase p95 latency was 195.09 ms at 5 offered requests/second, 290.55 ms at 10, and 199.31 ms at 20; each phase lasted 10 seconds. The non-monotonic figures are a reason to retain raw samples and avoid extrapolating a capacity ceiling. Maximum scheduling lag was below 0.83 ms. The database-counter deltas also include the observation connection and background/statistics effects; they are not per-request query counts.


`verify_multiworker.py` exports every tracked byte of pinned commit `d4a9d852ea2bfe7c28589daea9a5a946f07279bf` to reproduce the original two-worker quota amplification. It then applies the current access-only migration to that actual original schema and requires every ledger/account column fingerprint to remain unchanged. Two actual Uvicorn processes serve distinct loopback ports; the client explicitly alternates them. There is **no load balancer** in this experiment.

The fault harness wraps the real ASGI application only in the exercise process. It withholds the HTTP response after the real endpoint returns, exposes a private process marker, independently observes the committed row through a separate database connection, and kills that exact serving PID. Neither the application nor the public demo has a fault-control endpoint. Six earlier acknowledged events remain, the ambiguous event appears once, and the survivor's statement reconciles to 77 units / 19,250 microUSD. A restarted worker must still reject an exhausted account window.

The separate sustained-load phase offers 5, 10 and 20 requests/second for a bounded duration, with an eight-client cap, six accounts and explicit worker routing. Its mix is 70% usage writes, 20% statement reads and 10% statement publications. Every acknowledged usage event is reconciled once by account, and final statements are checked against independently summed integer units at the known fixture price. Per-request records retain target rate, worker/account index, method/path, status, latency and scheduling lag without tokens or actual account identifiers.

## Reproduce and migrate

Use an isolated PostgreSQL 17 **cluster**, a control database ending in `_test`, a checkout with the pinned baseline commit available, and `requirements-dev.txt`. Supply `TEST_DATABASE_URL` privately, then:

```sh
python -m pytest -q
python verify_multiworker.py --phase-seconds 10 --output local-evidence/multiworker-new.json
```

The harness creates/drops its own generated database and provisions cluster-global test roles/passwords. Do not run it against a cluster hosting an active application. It refuses to overwrite an existing evidence path. The hosted workflow runs this gate and retains its fresh report beside the older HTTP, privilege and restore gates. Historical JSON reports stay unchanged and are checked against their own source revisions.

For a real installation migration, stop all old API workers first. Apply `configure_runtime` with the administrative connection in a separate process, then start the new workers using only the restricted runtime DSN. The migration adds the private table and fixed function; it does not reseed or rewrite financial history, and repeat provisioning does not clear existing database windows. The first transition from in-memory quotas necessarily starts new database windows; draining old workers avoids mixed enforcement. Credential provisioning remains cluster-global, so coordinate that existing boundary explicitly. Back up trusted data before operational changes. The current fresh-database restore gate also re-establishes ownership/ACLs for the sixth function and the private table.

## Limits and next operating evidence

- Synthetic local/CI traffic, one PostgreSQL node, two local API workers. No hosted API, TLS/proxy, internet latency, customer traffic, failover or availability claim.
- Authentication still occurs before admission. The limiter cannot bound invalid-bearer authentication or connection-creation costs, and it does not provide denial-of-service protection. Shared compute, database and 64-sandbox capacity remain shared resources.
- The limit bounds logical rows/timestamp arrays, **not disk bytes**: PostgreSQL MVCC writes still need vacuum and disk monitoring. The per-kind admission lock and connection-per-operation design limit throughput.
- The clock is PostgreSQL wall time; a large clock adjustment changes expiration behavior. A conservative 60-second retry hint is retained for 429 responses.
- Database-pressure observations sample `pg_stat_activity` every 50ms; short peaks can be missed. Database counter deltas update asynchronously. The bounded offered load is not a measured maximum capacity, production SLA or evidence of sustained adoption.
- Worker death after observed commit is tested. Database restart, failover, power loss and database restore during live traffic are separate boundaries. AI-assisted implementation/tests do not establish independent review or unaided interview performance.

## Applied delivery prompt and primary references

The existing [evidence-first prompt](DELIVERY_PROMPT.md) was narrowed to this acceptance contract:

> Reproduce the existing multi-process failure before changing it. Keep the ledger's immutable event identity and restricted runtime boundary. Implement the smallest shared, bounded admission mechanism; prove its concurrency, expiry, restart and privilege behavior. Synchronize process failure with an observed database commit, then reconcile acknowledged and ambiguously delivered events independently. Measure a declared finite workload, preserve per-request evidence and distinguish local measurements from capacity or production claims. Preserve prior reports and document migration and remaining costs.

Design references: PostgreSQL [transaction-level advisory locks](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS), [safe SECURITY DEFINER functions](https://www.postgresql.org/docs/17/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY) and [statistics snapshot/refresh behavior](https://www.postgresql.org/docs/17/monitoring-stats.html). These support implementation decisions; no prompt establishes correctness or security by itself.
