"""Build deterministic public downloads from reviewed local sources."""

import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["__init__.py", "private.py", "local.py", "client.py", "sample.json", "README.md"]


def archive():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name in FILES:
            info = zipfile.ZipInfo("starter/" + name, (2026, 11, 15, 0, 0, 0))
            bundle.writestr(info, (ROOT / "starter" / name).read_bytes())
        bundle.writestr(
            zipfile.ZipInfo("docs/FIRST_USE.md", (2026, 11, 15, 0, 0, 0)),
            (ROOT / "docs/FIRST_USE.md").read_bytes(),
        )
    return output.getvalue()


if __name__ == "__main__":
    destination = ROOT / "web/start"
    destination.mkdir(exist_ok=True)
    (destination / "meterwork-starter.zip").write_bytes(archive())
    (destination / "sample.json").write_bytes((ROOT / "starter/sample.json").read_bytes())
    print("Starter archive and synthetic API template refreshed; no local state included.")
