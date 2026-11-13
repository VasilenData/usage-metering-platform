# Runtime credentials and account request allowances

Meterwork's HTTP process should acknowledge usage and publish statements without being able to create database roles, rewrite prices or choose an account by guessing its ID. A noisy authenticated account should not spend another account's request allowance. This upgrade makes those boundaries executable while preserving the existing ledger and historical evidence.

**Scope:** synthetic PostgreSQL and one-process loopback HTTP exercises. The public website remains a read-only snapshot with no hosted API. Implementation and review are AI-assisted; this is not an external security assessment or evidence of actual customers.

## Observed result — 13 November 2026

[CI run 43350733026](https://github.com/VasilenData/usage-metering-platform/actions/runs/43350733026) passed on source [`6f65a5d`](https://github.com/VasilenData/usage-metering-platform/tree/6f65a5da4c862c6611af4e471eb3e0f4e4de2f63). The downloaded evidence ZIP matched GitHub's SHA256 digest `169a194b48b061642e4d1f982c31610fba92f629fbebed735cda301c56064dba`. The [runtime result](runtime-security-2026-11-13.json), [HTTP result](http-runtime-2026-11-13.json) and [Store recovery result](release-runtime-2026-11-13.json) are unchanged artifact copies; their source hashes match the measured implementation.

| Evidence | Observed result and scope |
|---|---|
| Full Python suite | **126 passed**, zero failures/errors/skips, including the explicit mocked authentication-cost counterexample. |
| Runtime acceptance | **27 passing checks** covering actual PostgreSQL permissions, privilege drift, token-derived row access and real HTTP request allowances. |
| Fairness / proxy-header exercise | **363 loopback HTTP requests**: anonymous 240×401 +1×429; A 120×200 +1×429; B 1×200 from the same peer. Rotating forwarded headers did not reset anonymous admission. |
| Separate HTTP regression exercise | **345 requests**, zero current-release 5xx, 19 passing assertions; includes eight-client replay/conflict and correction/publication races. The three intentionally reproduced historical 500s are excluded. |
| Separate Store recovery exercise | **15 passing checks**, including disconnected-client rollback and backup restoration of fixture snapshots. This does not validate a deployed runtime-identity restore procedure. |

These are synthetic GitHub-hosted Linux/PostgreSQL 17 exercises with one Uvicorn process at a time. They demonstrate the stated boundaries, not internet traffic, a capacity benchmark or an independent human security review.

## Credential boundary

| Identity / component | Allowed responsibility | Excluded from HTTP |
|---|---|---|
| Explicit `bootstrap_runtime.py` process | Create/review roles, initialize schema, seed the public demo, install fixed control functions | Administrative DSN and runtime-password setup variable |
| `meter_api_runtime` login | Invoke five fixed functions; select rows in its authorized scope; append usage, product events and statements | Role/schema creation, other role membership, relation ownership, token hashes, price writes, ledger deletion and function replacement |
| `meter_control` NOLOGIN owner | Execute the bounded function bodies with explicitly granted table permissions | Interactive login, superuser/BYPASSRLS authority and client-selected arbitrary SQL |
| Existing offline `Store` | Seed fixtures, provision price versions, reproduce historical measurements and restore exercises | Import/initialization as the HTTP connection store |

The HTTP lifespan requires the dedicated `meter_api_runtime` identity and rejects privileged flags (including REPLICATION), role memberships, owned relations, unsafe schema/database creation, mutable ledger grants, unexpected executable control functions or disabled forced RLS. PUBLIC privileges are included in the effective-permission checks; TRUNCATE bypasses RLS and is explicitly forbidden. It rejects the known administrative/test DSN environment variables rather than falling back to them. The admin credential is supplied to a separate bootstrap process; credentials are never derived from the admin password inside HTTP.

Only the five named function signatures receive runtime execution, including when bootstrap runs against an existing schema. An unrelated pre-existing helper must stay inaccessible. The control functions have a fixed `pg_catalog` search path, explicitly qualified application tables and no PUBLIC execution. Their owner cannot log in. Runtime cannot replace the functions or become their owner. Sandbox provisioning is the deliberately narrow exception to direct price-write denial: it creates one new random account, its one-hour expiry and two fixed seed prices, subject to the shared 64-account cap. It cannot set prices or identity for an existing account.

## Why the account selector changed

The legacy offline role used `app.tenant_id`. PostgreSQL sessions can set custom configuration variables; possession of that legacy database credential therefore allows choosing another UUID. `verify_runtime.py` reproduces this counterexample directly against the unchanged legacy role. That role and its administrative fixture tools are not the HTTP runtime.

The new policy ignores `app.tenant_id`. `meter_api.current_tenant()` hashes the transaction-local bearer, checks its expiry and derives the account from the private token table. The runtime cannot read that table. Selecting a UUID without a token yields no account rows; using A's token with B's UUID still exposes only A. Public seeded demo reads are intentionally accessible in a separate read scope; no bearer grants demo writes.

This is a bearer model: possession of a valid account token authorizes that account. It does not protect against token theft or a compromised app host that can observe active request tokens. A compromised runtime can also create bounded sandbox accounts and consume shared resources. No row policy can make a compromised application process or an administrator harmless.

## Separate, bounded request allowances

- Authenticated account: **120 API admissions per rolling 60 seconds**, keyed by the validated database account ID.
- Unauthenticated client: **240 per rolling 60 seconds**, keyed by the direct TCP peer. Invalid JSON rejected before authentication uses this allowance too.
- At most **256 active buckets per kind**; expired entries are reclaimed, active entries are never evicted to reset their quota. Accounts and anonymous peers cannot consume each other's state slots.
- Uvicorn runs with `--no-proxy-headers`. Changing `X-Forwarded-For` must not manufacture new peers. Configure a genuine trusted-proxy contract before changing this launch mode.

An exhausted account receives 429 with `Retry-After: 60`; another authenticated account on the same TCP peer keeps its own allowance. Clients behind the same peer share the anonymous allowance. Authentication still requires a database lookup before account admission, and all accounts share compute, database capacity and the 64-sandbox limit. The mocked-lookup counterexample `test_admission_quota_does_not_bound_invalid_bearer_authentication` makes 245 distinct invalid-bearer requests: 240 return 401 and five return 429, but all 245 reach the authentication method. This is an in-process method-call observation, not a measurement of live database connections. A pre-auth peer gate would also block a previously unseen valid account on the same peer; resolving that trade-off needs a separate gateway/authentication design. These controls are admission fairness for one process, not distributed rate limits or a denial-of-service guarantee. Restarting the process resets the in-memory windows; multiple workers would multiply them.

## Reproduce the acceptance exercise

Use a disposable PostgreSQL 17 database whose name ends in `_test`, supply its administrative URL privately as `TEST_DATABASE_URL`, install `requirements-dev.txt`, then run:

```sh
python verify_runtime.py --output local-evidence/runtime.json
```

The fixture command bootstraps the database, but its real Uvicorn child receives only the runtime DSN. It checks denied role/schema/function/price/ledger operations, tenant-selector forgery, cross-account writes, expiry, a matching-token temporary-table shadow and function ACL/ownership. Temporary administrative changes add REPLICATION, PUBLIC TRUNCATE, account DELETE and unexpected helper execution; each must make startup reject the identity before the test restores the original privileges. It then makes 241 anonymous requests with rotating forwarded headers, 121 requests as A and one as B: **363 synthetic HTTP requests**. It requires 240 anonymous 401 responses then 429, 120 A successes then 429, and B success from the same peer after both limits are exhausted. The attached raw runtime result records these observed outcomes on the source revision above.

CI publishes `runtime-ci.json` alongside the existing HTTP and recovery artifacts. The report includes each check, run/source identifiers and exact file hashes, excluding tokens and DSNs. Earlier raw JSON files remain immutable. The existing backup exercise validates offline Store snapshots and RLS; it does not establish preservation of the new function ownership under every restore procedure. A production restore must reapply and verify the runtime role/function ownership before traffic.

## Human review and next operating evidence

Review requests remain drafts. No external reviewer or finance stakeholder is represented as having approved this release. A useful review would attempt a new account-boundary counterexample, inspect the SECURITY DEFINER grants and demonstrate a correction/publication race. Preserve the real review discussion and any resulting change after it happens.

Live operation, external testing, TLS/proxy configuration, credential rotation, backup procedures for deployed identities and a distributed admission layer remain outside this local/CI demonstrator. The owner should also explain and change the code unaided; passing AI-assisted tests does not establish that performance.
