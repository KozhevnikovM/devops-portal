#!/usr/bin/env python3
"""Run npm audit and compare reported vulnerabilities against a committed baseline allowlist."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".npm-audit-baseline.json"


def load_baseline(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        return set(data)


def main() -> int:
    baseline = load_baseline(BASELINE_FILE)
    print("Running npm audit --json...")
    res = subprocess.run(
        ["npm", "audit", "--json"], capture_output=True, text=True, check=False
    )

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse npm audit output as JSON: {exc}", file=sys.stderr)
        if res.stderr:
            print(res.stderr, file=sys.stderr)
        return res.returncode or 1

    vulns = data.get("vulnerabilities", {})
    if not vulns:
        print("✓ No npm vulnerabilities found.")
        return 0

    reported_advisories: set[str] = set()
    for info in vulns.values():
        for via in info.get("via", []):
            if isinstance(via, dict) and "url" in via:
                url = via["url"]
                advisory_id = url.rstrip("/").split("/")[-1]
                reported_advisories.add(advisory_id)

    unapproved = reported_advisories - baseline
    if unapproved:
        print(
            f"FAIL: Found {len(unapproved)} unapproved npm vulnerabilities:",
            file=sys.stderr,
        )
        for adv in sorted(unapproved):
            print(f"  - {adv}", file=sys.stderr)
        return 1

    print(
        f"✓ All {len(reported_advisories)} reported npm vulnerabilities match baseline allowlist."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
