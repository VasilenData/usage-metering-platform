# First useful result: explain a corrected statement

This local integration starter is for a developer evaluating Meterwork's HTTP API. It uses the existing restricted API, Compose bootstrap and ledger. The public website is a static guide with synthetic downloads: **there is no hosted backend**. No cloud account, API key or paid resource is needed. Docker downloads require internet access. Confirm Docker's license terms apply to your environment; Docker Engine with Compose is also supported.

## Clean installation

Supported path: Python 3.12+, Git and Docker Engine with Compose v2 (Linux), or Docker Desktop using Linux containers (Windows/macOS). The full repository is required for Compose. Python uses only its standard library for the starter; container dependencies are installed by the existing Dockerfile. Use a local folder owned by your user, outside shared/synced folders.

```sh
git clone https://github.com/VasilenData/usage-metering-platform.git
cd usage-metering-platform
python -m starter.local doctor
python -m starter.local setup
python -m starter.local up
python -m starter.client
```

Use `python3` on macOS/Linux or `py -3.12` on Windows if `python` does not select Python 3.12+. `doctor` identifies missing Compose or a stopped/inaccessible Docker daemon. `setup` can run without Docker; it writes separate random local development passwords to ignored `.env` only when absent. It never prints them or replaces existing credentials. Newly created POSIX files use mode 600; newly created Windows files receive a current-user ACL before secret bytes are written. Existing files are validated and preserved; existing Windows ACLs are your responsibility and are not changed or certified. Do not run as administrator/root or share `.env` or `.meterwork/`. Both are excluded from Git and Docker builds.

`up` reuses `compose.yaml`: PostgreSQL is private to Compose; administrative bootstrap finishes before the API starts; the app receives only the restricted runtime DSN. Only `127.0.0.1:8000` is published. If port 8000 is occupied, stop the conflicting local service. Do not expose a port or add administrator credentials to the application to work around startup errors. Exported password variables can override Compose's env file, so the starter refuses exported `POSTGRES_PASSWORD`/`METER_RUNTIME_PASSWORD` before launch. Keep any preexisting `.env` with its original data volume.

Open <http://127.0.0.1:8000> for the existing browser console/SDK; local API fields are at `/openapi.json`. The Python example is a repeatable integration of the same endpoints.

## What the example proves

Input is the fixed **synthetic** `starter/sample.json`, not an arbitrary billing-data importer. One sandbox account; source `meterwork-starter`; immutable IDs `starter-original-v1` and `starter-correction-v1`; occurrence `2026-10-03T12:00:00Z`; October 2026 UTC half-open month; integer units and microUSD. A correction keeps the original occurrence/source and refers to its original ID. The effective price is 250 microUSD/unit. No taxes, payments, FX or invoice rounding are implied.

1. Create one expiring sandbox and persist its bearer token privately.
2. Send 12,000 units, publish revision 1: 3,000,000 microUSD = USD 3.00.
3. Replay exactly the same event: duplicate acknowledgement, no extra charge.
4. Send a new correction of -2,000 units, publish revision 2: 2,500,000 microUSD = USD 2.50.
5. Read both revisions and ledger totals from the API. Independently reconcile `12,000 - 2,000 = 10,000` units and `-2,000 × 250 = -500,000` microUSD.

Export: `.meterwork/first-use/result.json`. It contains both complete API statements, their source fingerprints and rated lines, the two inputs, integer reconciliation and readable USD totals. Output grain is one two-revision journey per account/month; statement lines remain one row per event. No bearer token or development password is exported. Failed validation cannot replace a complete result. Files are staged/flushed before a same-filesystem atomic rename; this is not a power-loss durability claim.

## Rerun and recovery

Rerun `python -m starter.client` to reuse the same account and immutable payloads. Statement IDs and result bytes remain unchanged. The duplicate-attempt counter can increase; duplicate events never add units. One client owns a workspace at a time. If forcibly stopped, confirm the process exited, then remove only that workspace's `.lock` before retrying.

Usage and statement requests get at most three attempts with identical serialized bytes. Lost responses/exhausted 5xx retries are **UNKNOWN**, since a commit may have happened. The old revision is saved before correction. Sandbox creation has no replay key and is never automatically retried: interrupted provisioning stops, even if an account might exist. Explicitly choose another workspace after acknowledging that condition:

```sh
python -m starter.client --workspace next-demo
```

Tokens expire after one hour. The API may clean up expired sandbox rows; local exports remain. Expiry does not silently create another account. Keep private state while recovering; do not delete it to fix an unknown write. A 429 asks you to wait one minute and rerun. HTTP 4xx stop immediately. `--base-url http://127.0.0.1:PORT` supports manual local installations; public hosts, URL credentials, proxies and redirects are rejected/disabled. The existing browser SDK remains at `web/sdk.js`.

## Stop, restart, backup, upgrade

```sh
python -m starter.local stop
python -m starter.local start
python -m starter.client
python -m starter.local backup
```

`stop` retains containers and the named `meterdata` volume. `start` starts them and checks health. `up` builds/recreates containers while preserving the volume. None deletes data. Backup uses PostgreSQL 17's `pg_dump` inside the existing container, writes a uniquely named private `.meterwork/backups/*.dump`, and publishes only after a successful dump with a valid archive header. Backups contain account data; keep them private. Archive validity is not proof of recoverability: rehearse restoration before upgrading. [RUNTIME_RESTORE.md](RUNTIME_RESTORE.md) covers controlled restore, role/ACL repair and the executable synthetic restore proof.

For a code upgrade: record `git rev-parse HEAD`; export needed statements; run `backup`; retain `.env`, backup and old source commit together privately. Stop the app before backup if writers are possible (`docker compose stop app`; PostgreSQL must stay running). Check out the reviewed target commit, read migration notes, then run `python -m starter.local up`. Confirm `/healthz`, read retained statements and run the sample in a new workspace. This demonstrator has no general automatic migration/rollback framework. Bootstrap reapplies the reviewed current schema/access functions; arbitrary versions are not guaranteed compatible. On failure, stop the app and restore into a separate PostgreSQL 17 database/volume under pinned old source following the recovery guide. Never overwrite the only good volume. Do not upgrade PostgreSQL majors by retagging the image; use a tested dump/restore or PostgreSQL-supported upgrade.

## Explicit destructive reset (optional)

Only to intentionally erase **all local Meterwork database data** for this checkout's Compose project: first save/verify required backups and inspect `docker compose ps` and `docker volume ls`. From the original checkout, run `docker compose down --volumes`. This removes the project's named volume and is never run by starter commands. Keep `.env`; `up` initializes an empty database with the same development credentials. Old tokens then refer to deleted accounts; use a new workspace. Removing `.env` while retaining a volume creates a credential mismatch, not a reset.

## Verification

[Observed local results and explicit platform limits](FIRST_USE_VERIFICATION.md): 201 Python tests passed, two platform skips, eight real HTTP/SQL starter checks, and all existing integration gates passed. Docker was unavailable locally; Compose is a separate required CI job.

`pytest -q tests/test_starter.py` covers setup/errors/permissions/paths/retries/export invariants without Docker. `python -m starter.verify` uses an isolated disposable PostgreSQL database and real loopback Uvicorn with `TEST_DATABASE_URL` ending in `_test`. It checks first execution, exact rerun and independent SQL. The dedicated `compose-first-use` CI job exercises full Compose setup, repeated setup, run/rerun, stop/start and backup. Historical reports stay pinned to their measured sources.
