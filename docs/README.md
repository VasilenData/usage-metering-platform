# Evidence and contracts

Read [the contract](CONTRACT.md) for billing semantics and isolation limits, [API reliability](API_RELIABILITY.md) for the real-HTTP exercise and human-review packet, [product definitions](PRODUCT_ANALYTICS.md) for metrics and experiment gates, and [the delivery prompt](DELIVERY_PROMPT.md) for attributed engineering references.

[November 12 recovery evidence](release-2026-11-12.json) records the historical local run. [November 13 HTTP evidence](http-2026-11-13.json) and [November 13 recovery evidence](release-2026-11-13.json) record the verified GitHub-hosted Linux run described in [API reliability](API_RELIABILITY.md). New CI runs attach current HTTP, recovery and workload evidence to the `postgres-test-evidence` artifact, with explicit host/source metadata. Preserve those distinctions when citing results.
