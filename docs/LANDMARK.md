# Case study: explain a changed usage statement

An API business receives retried requests, late usage and corrections. A pricing update can arrive today but apply to an earlier business date. The finance team needs to explain the revised amount without rewriting what the customer originally received.

Meterwork is an independent implementation of that problem. All accounts and traffic in this case study are generated. No money is collected. The public website is a read-only result; the FastAPI/PostgreSQL application runs locally.

## The engineering decisions

1. **Identity before aggregation.** `(tenant, source, event ID)` is the immutable usage identity. Exact replay returns a duplicate; changed content is rejected. Reused IDs in other accounts are independent events.
2. **Two clocks.** `effective_from` selects the price at the event's business time. PostgreSQL's `recorded_at` determines whether that price was known at publication. Each statement records `known_at`. A later backdated price cannot change the reconstruction of an earlier statement.
3. **A conserved explanation.** `statement_diff.py` validates the document fingerprints and amounts before comparing revisions. Late usage, signed usage corrections and repricing of existing lines explain the exact change. A missing old line, altered immutable event, wrong account or reversed revision is rejected.
4. **One local transaction boundary.** Publication, ingestion and administrative price provisioning acquire the same tenant lock. Unrelated tenants can proceed independently. The statement is committed completely or not at all; the previous version remains available.
5. **An independent check.** `verify_workload.py` compares each statement with separately written PostgreSQL SQL and fixture arithmetic. It does not call `billing.rate` to calculate the expected total.

## Reproduce the workload

Use a dedicated PostgreSQL database ending in `_test`. Set `TEST_DATABASE_URL` privately, install `requirements-dev.txt`, then run:

```sh
python verify_workload.py --accounts 24 --originals 450 --clients 8 --output local-evidence/workload.json
```

Each of 24 accounts receives 450 original events, 20 late events and 20 signed corrections. Forty original requests are retried, and one changed-payload replay is rejected per account. Occurrence times include both UTC month boundaries and the effective pricing boundary. Arrival order is deterministic and shuffled. The account's 490 accepted events stay within the existing 500-event sandbox limit.

The exercise publishes an original revision, a late/corrected revision and a revision after a backdated price is provisioned. It then rechecks all three against their historical knowledge cutoffs. All accounts created by this run are cleaned up in `finally`; unrelated accounts are not removed.

The committed [measured result](../web/landmark.json) records parameters, source hashes, runtime, each check, account totals and an event-level explanation for the first synthetic account. The UI displays this result and fails visibly if its summary does not reconcile. CI also executes a smaller two-account version; it does not pretend that the committed larger measurement ran on every push.

## What this proves—and its limits

- The tested fixture reconciles across independently implemented calculations and historical revisions. Input and code hashes make the scope inspectable.
- Latencies include the local Python storage call, connection setup, tenant lock and transaction. They exclude HTTP, the browser and network deployment. The run is bounded; it is not a saturation test, production throughput claim or sustained operating history.
- The SQL oracle and application use the same database. The separate fixture arithmetic provides an additional check but cannot prove correctness for every possible input. Hand-computed boundary cases and property tests cover other cases.
- Prices remain administrative and append-only. The current contract adds a new effective boundary; it does not silently replace a price at an existing boundary. Taxes, tiered pricing, FX, payment collection and legally binding invoicing are outside scope.
- The dated workload uses the offline administrative Store and its legacy selector. The [subsequent HTTP runtime](RUNTIME_SECURITY.md) uses a separate credential and bearer-derived RLS. The workload results are not a penetration test or a measurement of that later runtime.
- Existing fixed seed prices are migrated using tenant creation time. That is appropriate only for this project's documented old provisioning behavior; it is not a method for recovering missing historical knowledge timestamps in arbitrary systems.
- Legacy statements used the application's publication clock. Unknown historical app/database clock skew or manual administrator changes cannot be reconstructed by this migration. New statements and price receipts share the database clock.

## Primary design references

[OpenMeter usage events](https://openmeter.io/docs/metering/events/usage-events) informed the identity and replay problem. [PostgreSQL transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html) and [row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) inform the implementation boundary. These references are not claims of equivalent scale or feature coverage. The [delivery prompt](DELIVERY_PROMPT.md) records the selection and verification criteria.
