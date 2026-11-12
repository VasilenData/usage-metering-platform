# Metering and publication contract

## Grain, clocks and money

One immutable usage row is identified by **(tenant, source, event ID)**. Exact semantic replay returns `duplicate`; reused identity with changed content is a conflict. The server derives tenant identity from a hashed, expiring bearer token. A JSON tenant field is rejected.

Occurrence timestamps require an offset and are normalized to UTC. Receipt time is assigned by PostgreSQL. Billing periods are half-open UTC calendar months. A late event is rated in its occurrence month and produces a new statement revision when published; it never edits a prior statement. Sandbox public input accepts dates from 2026 onward and at most five minutes ahead of the server clock.

Units and prices use integers. Prices are micro-US dollars per unit (1 USD = 1,000,000 μUSD). Display rounding is not a calculation input. The two synthetic price versions are 250 μUSD from 1 January 2026 and 200 μUSD from 15 October 2026. Version selection uses occurrence time, never arrival time. There are no taxes, payment collection, currency conversion, credit-risk decisions, prepaid balances or legally binding invoices. Published outputs are **estimated usage statements**.

Corrections are append-only signed deltas referencing an original in the same tenant and source. They must preserve its occurrence time. Corrections cannot reference another correction, and the sum for an original cannot become negative. Tenant-scoped transaction locks make this check safe under competing corrections. Historical prices are read-only to the request identity.

## Reproducible pricing and restatements

Prices now have two clocks: `effective_from` says which usage occurrences a price covers, while database-assigned `recorded_at` says when the system learned that price. The administrative Python method `Store.provision_price` appends a new version under the same tenant transaction lock used by ingestion and publication. It is not an HTTP endpoint. Exact replay returns the existing version; changed content under the same ID or a second version at an existing effective boundary fails. Replacing a price at an identical boundary is deliberately unsupported; this project models newly introduced effective boundaries, not unrestricted contract amendment.

Each new statement stores a PostgreSQL `known_at` cutoff. Its usage receipts and price receipts must be at or before that cutoff. A backdated price can therefore affect a new revision without changing the recomputation of an older revision. `created_at` uses this same database clock, avoiding an application/database clock comparison. Cutoff metadata is outside the semantic content fingerprint: republishing identical source content still returns the existing revision.

Rating engine 1.1.0 includes price receipt metadata in its source fingerprint. The verifier retains engine 1.0.0 support for existing public documents, using their original four-column price fingerprints and their creation time as the legacy cutoff. Schema migration assigns existing seed prices their tenant creation time as a documented baseline. Earlier versions only provisioned those fixed prices at tenant creation; this does **not** recover an unknown history of manual administrative edits. Existing published documents are not rewritten by migration. A first publication under the new engine can create a new revision even when the monetary total is unchanged.

`statement_diff.compare_statements(before, after)` produces an event-level bridge for a later revision of the same tenant, month and currency. It verifies content fingerprints, line arithmetic and total conservation, and rejects removed or altered source events. The reasons are:

- `late_usage`: newly included original events, valued at the new revision's price.
- `usage_correction`: newly included signed corrections, also valued at the new price.
- `price_change`: repricing of existing original or correction lines, with zero change in units.

This attribution convention handles simultaneous corrections and repricing without double-counting. For example, 100 units at 250 μUSD become 120 units at 200 μUSD after 40 late units and a −20 correction: the bridge is +8,000 late usage, −4,000 correction and −5,000 repricing, exactly the −1,000 μUSD statement change. Fingerprints detect changed content; they are not signatures or proof that source events are truthful.

## Snapshots and transactions

Usage ingestion, product events and statement publication serialize within a tenant using a PostgreSQL transaction-scoped advisory lock. Different tenants can progress independently (a rare lock-hash collision only adds serialization). Runtime queries set the tenant context transaction-locally. Row-level security is enabled and forced; the runtime identity is not the table owner, superuser or BYPASSRLS role.

The statement contains each rated source event, price version, integer amount, engine version, source fingerprint and content fingerprint. An unchanged month reuses its existing revision. Unrelated months do not change the fingerprint. PostgreSQL commits a complete statement or none of it. A process-death exercise kills the client after an actual INSERT and before COMMIT; it tests disconnected-client rollback, not database power-loss or multi-region failover.

## Bounded demonstration

The seeded account is read-only publicly and wholly synthetic. A sandbox expires after one hour. At most 64 active sandboxes, 500 usage events and 500 product events per sandbox are allowed. Expired disposable accounts are removed when a new sandbox is created. The application has a fixed 240-request/minute API limiter, an 8KB request body ceiling, a 4KB product-event ceiling and five-second SQL statement timeout. Run one application replica; the rate limiter is process-local.

An administrative connection handles migrations, sandbox allocation, token lookup and expiry. Ledger/report queries use a separate restricted role. This separation protects against omitted tenant predicates; it is not a security boundary against a compromised application host, which can access its administrative environment credential. A production deployment should separate provisioning into a control-plane service, supply a separately rotated runtime credential and use an external distributed rate limiter and durable customer retention policy.

## Explicit limits

One PostgreSQL node, one application replica, no managed high availability, no real customers, no achieved revenue impact and no long-running production SLO claim. The experiment is a fixed demonstration protocol; synthetic observations cannot establish a business uplift. A local backup/restore exercise does not establish a cloud disaster-recovery guarantee. See dated release evidence for what was actually executed.
