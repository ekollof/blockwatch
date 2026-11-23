#!/usr/bin/python3
"""Demo helper for the Blockwatch screenshot.

It serves a committed fictional report for `status` and `poll` and touches no
user config or state. `demo/run` installs it temporarily over the real helper
and restores the real one from a backup on exit.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "/tmp/blockwatch-demo-served"


def main():
    command = sys.argv[-1] if sys.argv else ""
    if command not in ("status", "poll"):
        print(json.dumps({"ok": False, "error": "demo helper: unsupported command"}))
        return 1

    report = json.loads((HERE / "demo-report.json").read_text(encoding="utf-8"))
    report["checkedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report["events"] = []
    report["errors"] = []
    for row in report.get("addresses", []):
        row["newEvents"] = []
    try:
        Path(MARKER).write_text(command + "\n", encoding="utf-8")
    except OSError:
        pass
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
