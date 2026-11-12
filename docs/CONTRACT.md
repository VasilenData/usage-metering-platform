# Metering and publication contract

## Grain, clocks and money

One immutable usage row is identified by **(tenant, source, event ID)**. Exact semantic replay returns `duplicate`; reused identity with changed content is a conflict. The server derives tenant identity from a hashed, expiring bearer token. A JSON tenant field is rejected.

Occurrence timestamps require an offset and are normalized to UTC. Receipt time is assigned by PostgreSQL. Billing periods are half-open UTC calendar months. A late event is rated in its occurrence month and produces a new statement revision when published; it never edits a prior statement. Sandbox public input accepts dates from 2026 onward and at most five minutes ahead of the server clock.

Units and prices use integers. Prices are micro-US dollars per unit (1 USD = 1,000,000 μUSD). Display rounding is not a calculation input. The two synthetic price versions are 250 μUSD from 1 January 2026 and 200 μUSD from 15 October 2026. Version selection uses occurrence time, never arrival time. There are no taxes, payment collection, currency conversion, credit-risk decisions, prepaid balances or legally binding invoices. Published outputs are **estimated usage statements**.

Corrections are append-only signed deltas referencing an original in the same tenant and source. They must preserve its occurrence time. Corrections cannot reference another correction, and the sum for an original cannot become negative. Tenant-scoped transaction locks make this check safe under competing corrections. Historical prices are read-only to the request identity.

## Snapshots and transactions

Usage ingestion, product events and statement publication serialize within a tenant using a PostgreSQL transaction-scoped advisory lock. Different tenants can progress independently (a rare lock-hash collision only adds serialization). Runtime queries set the tenant context transaction-locally. Row-level security is enabled and forced; the runtime identity is not the table owner, superuser or BYPASSRLS role.

The statement contains each rated source event, price version, integer amount, engine version, source fingerprint and content fingerprint. An unchanged month reuses its existing revision. Unrelated months do not change the fingerprint. PostgreSQL commits a complete statement or none of it. A process-death exercise kills the client after an actual INSERT and before COMMIT; it tests disconnected-client rollback, not database power-loss or multi-region failover.

## Bounded demonstration

The seeded account is read-only publicly and wholly synthetic. A sandbox expires after one hour. At most 64 active sandboxes, 500 usage events and 500 product events per sandbox are allowed. Expired disposable accounts are removed when a new sandbox is created. The application has a fixed 240-request/minute API limiter, an 8KB request body ceiling, a 4KB product-event ceiling and five-second SQL statement timeout. Run one application replica; the rate limiter is process-local.

An administrative connection handles migrations, sandbox allocation, token lookup and expiry. Ledger/report queries use a separate restricted role. This separation protects against omitted tenant predicates; it is not a security boundary against a compromised application host, which can access its administrative environment credential. A production deployment should separate provisioning into a control-plane service, supply a separately rotated runtime credential and use an external distributed rate limiter and durable customer retention policy.

## Explicit limits

One PostgreSQL node, one application replica, no managed high availability, no real customers, no achieved revenue impact and no long-running production SLO claim. The experiment is a fixed demonstration protocol; synthetic observations cannot establish a business uplift. A local backup/restore exercise does not establish a cloud disaster-recovery guarantee. See dated release evidence for what was actually executed.
