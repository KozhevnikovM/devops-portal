#!/usr/bin/env python3
"""Run pip-audit against the declared requirements using baseline ignored vulnerabilities.

Audits requirements.txt (the application) and requirements-browser.txt (the pinned Playwright
used by the browser-tests CI job, #542).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".pip-audit-baseline.txt"
REQUIREMENTS_FILES = (ROOT / "requirements.txt", ROOT / "requirements-browser.txt")


def load_baseline(path: Path) -> list[str]:
    if not path.is_file():
        return []
    ignored: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            clean = line.strip()
            if clean and not clean.startswith("#"):
                ignored.append(clean)
    return ignored


def main() -> int:
    ignored_vulns = load_baseline(BASELINE_FILE)
    ignore_args: list[str] = []
    for vuln in ignored_vulns:
        ignore_args.extend(["--ignore-vuln", vuln])

    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        *ignore_args,
        *(arg for path in REQUIREMENTS_FILES for arg in ("-r", str(path))),
    ]
    if len(sys.argv) > 1:
        cmd.extend(sys.argv[1:])

    print(
        f"Running pip-audit with {len(ignored_vulns)} baseline ignored vulnerabilities..."
    )
    result = subprocess.run(cmd, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
