# Meterwork local starter

Extract this archive at the root of a full Meterwork repository checkout. It contains standard-library setup and API example code; the Dockerfile, Compose services and restricted HTTP API come from the full repository.

Run `python -m starter.local doctor`, `python -m starter.local setup`, `python -m starter.local up`, then `python -m starter.client`. Python 3.12+, Git and Docker Compose v2 are prerequisites. Use `python3` or `py -3.12` if needed. No hosted backend or cloud account. Full instructions: `docs/FIRST_USE.md`, included at its repository path in this archive.

The JSON sample is synthetic API request material, not a general importer. The fixed client reconciles USD 3.00 → 2.50 and preserves both revisions in `.meterwork/first-use/result.json`. Keep `.env` and `.meterwork/` private. Rerun for exact replay; a new `--workspace` deliberately provisions another expiring sandbox.
