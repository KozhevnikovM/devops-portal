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
    try:
        res = subprocess.run(
            ["npm", "audit", "--json"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        print(
            "FAIL: 'npm' command not found. Ensure Node.js and npm are installed.",
            file=sys.stderr,
        )
        return 1

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse npm audit output as JSON: {exc}", file=sys.stderr)
        if res.stderr:
            print(res.stderr, file=sys.stderr)
        return res.returncode or 1

    if "error" in data:
        err = data["error"]
        summary = ""
        detail = ""
        code = ""
        if isinstance(err, dict):
            summary = err.get("summary") or ""
            detail = err.get("detail") or ""
            code = err.get("code") or ""
        elif isinstance(err, str):
            summary = err
        msg = data.get("message") or summary or "npm audit failed"
        prefix = f"[{code}] " if code else ""
        print(f"FAIL: npm audit operational error: {prefix}{msg}", file=sys.stderr)
        if detail and detail != msg:
            print(detail, file=sys.stderr)
        if res.stderr:
            print(res.stderr, file=sys.stderr)
        return res.returncode or 1

    vulns = data.get("vulnerabilities")
    if vulns is None or not isinstance(vulns, dict):
        if res.returncode != 0:
            print(
                f"FAIL: npm audit exited with code {res.returncode} without vulnerability data.",
                file=sys.stderr,
            )
            if res.stderr:
                print(res.stderr, file=sys.stderr)
            return res.returncode
        print(
            "FAIL: Unexpected npm audit payload: missing vulnerabilities dictionary.",
            file=sys.stderr,
        )
        return 1

    if not vulns:
        if res.returncode != 0:
            print(
                f"FAIL: npm audit exited with code {res.returncode} but reported no vulnerabilities.",
                file=sys.stderr,
            )
            if res.stderr:
                print(res.stderr, file=sys.stderr)
            return res.returncode
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
