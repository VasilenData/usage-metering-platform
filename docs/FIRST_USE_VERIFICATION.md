# Local starter verification — 15 November 2026

Observed on Windows 11, Python 3.12.14 and PostgreSQL 17.11 in a newly initialized private cluster bound to a unique loopback port. No existing database was reused. The owned cluster was stopped after verification. This is a synthetic engineering exercise, not a timed user study or evidence of customer adoption.

| Check | Observed result |
|---|---|
| Final full `pytest -q` after both portability fixes | **205 passed, 2 skipped**, 73.05 seconds |
| Final starter tests, including ZIP platform regression | **39 passed, 2 skipped** |
| Real starter HTTP + SQL exercise | **8 checks passed** |
| Browser SDK and reconciliation tests | **10 passed** |
| Ruff lint / formatting / JS syntax / Git whitespace | Passed |
| Existing public snapshot | 2 statements reproduced, 5 usage events, 162 product events |
| Existing workload reconciliation | 11 checks; 160 accepted events, 80 replays, zero microUSD difference |
| Existing release / interruption / restore | 15 checks |
| Existing real HTTP boundary | 17 checks; 345 requests; zero 5xx responses |
| Existing restricted runtime | 27 checks; 363 requests |
| Existing fresh runtime restoration | 23 checks; 21 HTTP requests |
| Existing multiworker exercise | 32 checks; 350 requests |
| Existing finance export | 14 checks; 14 HTTP requests |
| Existing database-contention recovery | 17 checks; 14 HTTP requests |

The two skips are explicit platform limits: this Windows account cannot create a symlink for the symlink-path test, and POSIX mode enforcement is tested on POSIX only. Windows file ACL creation was exercised. The suite emitted one existing Starlette/httpx deprecation warning; no dependency was changed to suppress it.

The [first Linux CI run](https://github.com/VasilenData/usage-metering-platform/actions/runs/43774484018) found two portability defects that local Windows execution could not establish: an immediate connection reset while the newly started container initialized Uvicorn, and ZIP central-directory metadata that differed by authoring OS. The readiness probe now retries transport resets within the existing 90-attempt limit (two-second request timeout, one-second spacing); regression tests cover recovery and exhausted bounds. ZIP entries now set an explicit host OS and file mode, with a Windows/Linux metadata regression while preserving the strict byte-for-byte archive test. The final full and targeted suites cover both fixes. The final-head Linux CI run must establish the Compose outcome.

The [new raw starter receipt](starter-2026-11-15.json) fingerprints every core Python/SQL source and every starter Python module. It was generated before committing, so its source commit is honestly `local-uncommitted`; the hashes identify the exact code. CI recreates the receipt with `GITHUB_SHA`. The checks establish:

- A real restricted-API first run reconciles USD 3.00 → 2.50.
- An exact rerun preserves all exported bytes and both statement IDs.
- Independent PostgreSQL arithmetic yields two events, 10,000 units and 2,500,000 microUSD.
- Neither the sandbox token nor development password appears in the export.
- Expired credentials stop explicitly and preserve the last complete local result.
- Source bytes remain unchanged during the exercise.

The isolated local run also exercised credential preservation, missing Docker diagnostics, unignored/negated secret exclusions, hard-link refusal, traversal rejection, ACL failure before secret writes, response loss and incomplete-body retries with stable payloads, interrupted provisioning, failed atomic publication and failed-backup preservation.

Docker is not installed on this Windows machine. **The local result does not claim a Compose execution.** The dedicated `compose-first-use` GitHub Actions job separately runs setup twice, starts the actual Compose stack, completes/repeats the client, stops/starts while retaining data, and validates a private `pg_dump` archive. Pages publication requires that job plus the full existing verifier. See the run on the final published commit for its outcome; no hosted writable backend is deployed.

Reproduce with the commands in [FIRST_USE.md](FIRST_USE.md) and the existing [README verification section](../README.md#verify). Fresh integration reports are under ignored `local-evidence/`; the public starter receipt contains no credentials. Every older public receipt remains unchanged and is verified against its original pinned source.
