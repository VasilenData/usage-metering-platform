"""Run a synthetic event/replay/correction journey against the real local API."""

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from starter.private import (
    StarterError,
    atomic_json,
    exclusive,
    plain_path,
    private_dir,
    read_json,
    require_ignored,
)

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = {
    "id": "starter-original-v1",
    "source": "meterwork-starter",
    "occurred_at": "2026-10-03T12:00:00Z",
    "units": 12000,
    "correction_of": None,
}
CORRECTION = dict(ORIGINAL, id="starter-correction-v1", units=-2000, correction_of=ORIGINAL["id"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        valid = (
            url.scheme == "http"
            and url.hostname in {"127.0.0.1", "localhost", "::1"}
            and not url.username
            and not url.password
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
            and 1 <= (url.port if url.port is not None else 80) <= 65535
        )
    except ValueError:
        valid = False
    if not valid:
        raise StarterError("--base-url must be a loopback HTTP origin, for example http://127.0.0.1:8000.")
    return value.rstrip("/")


class Client:
    def __init__(self, base_url, token=None, opener=None, sleep=time.sleep):
        self.base_url = local_url(base_url)
        self.token = token
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.sleep = sleep

    def request(self, method, path, body=None, *, retry=True):
        # Serialize once. All attempts preserve the exact identity, timestamp and payload.
        data = json.dumps(body, sort_keys=True).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
            try:
                with self.opener.open(request, timeout=15) as response:
                    raw = response.read(1_000_001)
                    if len(raw) > 1_000_000:
                        raise StarterError("API response too large; local result remains unchanged.")
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise StarterError(
                        "Sandbox expired or unavailable. Existing result is preserved; use --workspace next-demo for a new sandbox."
                    ) from exc
                if exc.code == 429:
                    raise StarterError(
                        "Request allowance exhausted. Wait one minute, then rerun the same workspace."
                    ) from exc
                if exc.code < 500:
                    raise StarterError(
                        f"HTTP {exc.code} at {path}: request rejected. Check the API schema; the saved result is unchanged."
                    ) from exc
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                pass
            except (ValueError, UnicodeError) as exc:
                raise StarterError(
                    "Invalid API response; outcome may be unknown. Rerun the same workspace."
                ) from exc
            if attempt + 1 < attempts:
                self.sleep(0.25 * (2**attempt))
        raise StarterError(
            f"No confirmed response from {path}; outcome is UNKNOWN, not a confirmed rejection. "
            "Keep saved state and rerun the same workspace; usage retries keep their event IDs."
        )


def reconcile(old, new, history, overview):
    """Independent, hand-calculated oracle for this fixed sample, not the rating engine."""
    expected = [(old, 1, 12000, 3000000), (new, 2, 10000, 2500000)]
    for statement, revision, units, amount in expected:
        if (
            statement["month"] != "2026-10"
            or statement["revision"] != revision
            or statement["total_units"] != units
            or statement["amount_microusd"] != amount
        ):
            raise StarterError(
                "Statement does not reconcile with the fixed synthetic sample; previous export preserved."
            )
    if len(history) != 2 or old not in history or new not in history or old["id"] == new["id"]:
        raise StarterError(
            "Immutable statement history did not preserve both revisions; previous export preserved."
        )
    meter = overview["metering"]
    if (meter["accepted_events"], meter["total_units"], meter["estimated_amount_microusd"]) != (
        2,
        10000,
        2500000,
    ):
        raise StarterError("Ledger totals do not reconcile; previous export preserved.")
    events = sorted(meter["recent_events"], key=lambda event: event["id"])
    for event, expected_event in zip(
        events, sorted([ORIGINAL, CORRECTION], key=lambda event: event["id"]), strict=True
    ):
        if any(event[key] != expected_event[key] for key in ("id", "source", "units", "correction_of")):
            raise StarterError("Ledger event identity differs from the sample; previous export preserved.")
    return {
        "schema_version": 1,
        "scope": "Synthetic local API example; non-binding USD estimates, no payment or hosted backend",
        "month": "2026-10",
        "currency": "USD",
        "old_statement": old,
        "new_statement": new,
        "events": [ORIGINAL, CORRECTION],
        "reconciliation": {
            "original_units": 12000,
            "correction_units": -2000,
            "final_units": 10000,
            "unit_price_microusd": 250,
            "old_amount_microusd": 3000000,
            "new_amount_microusd": 2500000,
            "delta_amount_microusd": -500000,
            "old_usd": "3.00",
            "new_usd": "2.50",
            "delta_usd": "-0.50",
            "accepted_events": 2,
            "statement_revisions": 2,
            "exact_replay_accepted": False,
        },
    }


def run(base_url="http://127.0.0.1:8000", workspace="first-use", root=ROOT):
    base_url = local_url(base_url)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", workspace):
        raise StarterError(
            "--workspace must be 1–48 letters, digits, underscores or hyphens, beginning with a letter/digit."
        )
    require_ignored(root, ".meterwork")
    private = private_dir(plain_path(root) / ".meterwork")
    folder = private_dir(private / workspace)
    lock = folder / ".lock"
    try:
        exclusive(lock, "A starter client is using this workspace.\n")
    except FileExistsError as exc:
        raise StarterError(
            "Workspace is busy. If its process stopped, remove only its .lock file and rerun."
        ) from exc
    try:
        state_path, result_path = folder / "state.json", folder / "result.json"
        client = Client(base_url)
        if state_path.exists():
            state = read_json(state_path)
            if not isinstance(state, dict) or state.get("version") != 1 or state.get("base_url") != base_url:
                raise StarterError(
                    "Saved workspace has a different origin/version; preserved. Choose a new --workspace."
                )
            if state.get("phase") == "provisioning":
                raise StarterError(
                    "Sandbox creation was interrupted: outcome UNKNOWN. It has no replay key. "
                    "Keep this workspace for inspection; explicitly choose --workspace next-demo to provision another."
                )
            if not isinstance(state.get("token"), str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{24,256}", state["token"]
            ):
                raise StarterError("Saved token is invalid; preserved. Choose a new --workspace.")
        else:
            client.request("GET", "/healthz")
            state = {"version": 1, "base_url": base_url, "phase": "provisioning"}
            atomic_json(state_path, state)
            sandbox = client.request("POST", "/api/sandbox", retry=False)
            state.update(sandbox, phase="ready")
            atomic_json(state_path, state)
        client.token = state["token"]
        client.request("POST", "/api/usage", ORIGINAL)
        if "old_statement" not in state:
            state["old_statement"] = client.request("POST", "/api/statements", {"month": "2026-10"})
            atomic_json(state_path, state)  # Preserve old revision before attempting correction.
        replay = client.request("POST", "/api/usage", ORIGINAL)
        if replay.get("accepted") is not False or replay.get("duplicate") is not True:
            raise StarterError("Exact replay was not acknowledged as a duplicate; export preserved.")
        client.request("POST", "/api/usage", CORRECTION)
        new = client.request("POST", "/api/statements", {"month": "2026-10"})
        history = client.request("GET", "/api/statements?month=2026-10")
        overview = client.request("GET", "/api/overview")
        result = reconcile(state["old_statement"], new, history, overview)
        atomic_json(result_path, result)
        return result_path, result
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise StarterError(
            "State or API response failed validation; previous export preserved. Inspect the private workspace."
        ) from exc
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workspace", default="first-use")
    args = parser.parse_args()
    try:
        path, _ = run(args.base_url, args.workspace)
    except (StarterError, OSError) as exc:
        print(f"Example stopped: {exc}", file=sys.stderr)
        return 1
    print("PASS: 12,000 - 2,000 = 10,000 units; USD 3.00 -> 2.50; delta -0.50.")
    print("Exact replay charged once. Both immutable revisions reconciled. Local export: " + str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
