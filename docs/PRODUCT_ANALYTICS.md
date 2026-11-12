# Product analytics that can say “not enough evidence”

This is an engineering demonstration, not a running commercial experiment. The seeded actors are synthetic. Browser interactions occur in disposable demo workspaces; they are not customers. Neither source establishes business impact. The console always labels this scope.

## The actual application

When connected to the API, the console creates a one-hour sandbox only after an explicit action. “Start a new sandbox” captures a demo signup, requests a deterministic server assignment, displays the assigned onboarding instructions, then records exposure. The control shows a basic usage prompt; treatment adds a receipt/replay explanation. A separate user action sends a real request to the usage endpoint. The first accepted request records activation, each successful call records `api_call`, and an actual failure attempts to record `api_error`.

GitHub Pages serves a separate, explicitly read-only presentation. It loads the exported `web/demo.json`, shows its generation date, hides mutation controls and links to the local execution instructions. It does not create sandboxes, simulate successful API calls or claim that a backend is deployed. The `?mode=snapshot` query enables that same presentation for local visual verification. Relative asset paths support the repository's GitHub Pages subdirectory.

Assignment is separate from exposure; it does not imply that the user saw the experience. The event client waits until the assigned instructions are in the DOM before emitting exposure. It is a visibility approximation, not an attention measure. Refreshing the page creates a new in-memory actor identity; there is no cross-device identity resolution or cookie-based tracking.

The SDK reuses an event identity during bounded retries, avoids retrying permanent 4xx responses, and keeps credentials in memory. Accepted usage remains accepted even if the separate analytics write fails; the UI displays that instrumentation warning. An analytics outage can still bias observed outcomes, so the report is not sufficient to authorize a commercial launch.

## Contracts and clocks

The grain is one immutable `event_id` within a tenant. Events contain `actor_id`, a constrained event name, timezone-aware `occurred_at`, server-assigned `received_at`, and optional experiment metadata. `analytics.build_report` requires one tenant and rejects mixed-tenant input even when the offending event is outside the reporting window.

The reporting cutoff applies to **both** clocks. An event that happened yesterday but arrived tomorrow cannot appear in today's known-at snapshot. Duplicate IDs with identical payloads are excluded; changing a payload under an existing ID fails. Earliest receipt is retained when identical replay records are supplied to the pure analytical function.

| Metric | Definition |
|---|---|
| Actors | Distinct actors with at least one behavioural event; assignment/exposure metadata alone does not count. |
| Sessions | A new session after more than 30 minutes between behavioural events. Exactly 30 minutes stays in the session. Assignment/exposure cannot extend it. |
| Funnel | Distinct actors with ordered signup → activation → API call across available history. Steps can share a timestamp. There is no maximum conversion window in this descriptive funnel. |
| Day 1 retention | An activation or API call on the next UTC calendar day after first signup. Only actors whose entire next UTC day has elapsed enter the denominator. |
| Experiment conversion | At least one activation in the half-open 24-hour window beginning at the first eligible exposure. Earlier activity and activity exactly at the upper boundary are excluded. |
| Error guardrail | Fraction of mature exposed treatment actors with at least one API error in that same window. |

Unknown denominators produce `null`, displayed as “—”, rather than a misleading zero percent. The report contains source and protocol SHA-256 fingerprints. They establish reproducibility of these inputs and definitions; they do not prove the original events were truthful.

## Fixed protocol and release decision

`analytics.DEFAULT_PROTOCOL` is a versioned teaching protocol, committed with the code before any public collection in this application. The synthetic fixtures were designed to exercise it. It is not a claimed preregistration of a real commercial trial.

- Experiment: `onboarding-v1`, protocol version `1`, 50:50 assignment.
- Assignment: SHA-256 of UTF-8 `experiment_id:version:actor_id`; first eight bytes interpreted as an unsigned big-endian integer; modulo 10,000; values below 5,000 select control.
- Declared calendar window: 1 November through 1 December 2026 UTC, with a further 24 hours for outcomes. These fixed dates are intentional; changing them requires a new protocol version and evidence review.
- Primary criterion: treatment's absolute activation-rate lift is at least five percentage points and the approximate 95% difference interval is above zero.
- Quality gates: consistent assignments, valid matching exposures, a closed horizon, at least 100 mature exposed actors per arm, no assignment sample-ratio mismatch at alpha 0.01, and no treatment error rate above 5%.
- **Synthetic scope always blocks a launch decision**, irrespective of effect size. Real-instrumentation mode can return `eligible_for_review`, never an automatic launch instruction.

The minimum sample is a demonstration floor, **not a power calculation**. There is no optional stopping, peeking-based early launch, CUPED, multiple-primary-metric selection or simulated pre-experiment covariate. Assignment SRM uses an exact two-sided binomial test for 50:50 allocation. Arm intervals use Wilson scores; the difference uses the Newcombe unpooled score construction. Those intervals describe sampling uncertainty under their assumptions, not missing instrumentation or identity errors. The code does not measure network delivery completeness, exposure-selection bias, interference, bot activity or long-term guardrails; a real trial needs those checks and a prospective power analysis.

## Verification

Run `pytest tests/test_analytics.py` and `node --test tests/test_sdk.mjs`.

The independent fixtures cover ordered versus out-of-order funnels, calendar/timezone boundaries, immature cohorts, late receipt, mixed-tenant rejection, conflicting identities, assignment/exposure failures, pre-exposure and exact-window-boundary outcomes, hand-calculated SRM examples, synthetic decision blocking, guarded real-mode review, replay identity reuse, bounded retries and analytics failures after accepted usage. These tests are contract checks; they are not evidence of user adoption or experiment effectiveness.

## Reference material and adapted engineering brief

The architecture principles were researched from [PostHog's architecture](https://posthog.com/docs/how-posthog-works), [funnel definitions](https://posthog.com/docs/product-analytics/funnels), [retention definitions](https://posthog.com/docs/product-analytics/retention), and [experiment documentation](https://posthog.com/docs/experiments/statistics). This small system does not claim PostHog's scale or distributed architecture. Modularity, explicit validation and version-aware changes were also informed by the [official dbt analytics-engineering skill](https://github.com/dbt-labs/dbt-agent-skills/blob/main/skills/dbt/skills/using-dbt-for-analytics-engineering/SKILL.md).

The working prompt adapted those references into concrete acceptance criteria: define event grain, identity, clocks and ownership first; make the visible demo use real endpoints; separate assignment from exposure; publish metric definitions and immutable protocol fingerprints; test boundary cases with independent expected answers; preserve failures; and refuse to turn synthetic or insufficient data into a business claim. An impressive result comes from satisfying those criteria, not from claiming an internet prompt guarantees quality.
