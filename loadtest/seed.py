"""One-time setup for the 1000-user load test (#505, see loadtest/README.md).

Creates or reuses the test accounts (each with a quota high enough not to cap the test), one VM
image and hardware config, and a pool of static VMs and namespaces, through the admin API.

Idempotent: it lists what already exists first and creates only what is missing, so re-running it
against a seeded stack creates nothing and still exits 0. Every rejected create is reported, and
the script exits non-zero if there was any.

The target guard (loadtest/target_guard.py) runs before anything else: the target must be loopback
(or named in LOADTEST_ALLOW_REMOTE_HOST) and must report stub_terraform: true on GET /health.

Run: python loadtest/seed.py   (PORTAL_URL defaults to http://localhost:8000)
"""

from __future__ import annotations

import os
import sys
from http.cookies import SimpleCookie
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from target_guard import (
    ALLOW_REMOTE_HOST_ENV,
    TargetRefused,
    check_target,
)

BASE = os.environ.get("PORTAL_URL", "http://localhost:8000").rstrip("/")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

N_ACCOUNTS = 1000
ACCOUNT_PREFIX = "loadtest-"
# A fixed test-only credential: the guard only lets this run against a stub stack.
ACCOUNT_PASSWORD = "loadtest-pass-1234"  # nosec B105

IMAGE_NAME = "loadtest-image"
HW_CONFIG_NAME = "loadtest-small"

STATIC_VM_POOL_SIZE = 200
# A dummy: the stub stack never connects to these hosts.
STATIC_VM_PASSWORD = "loadtest"  # nosec B105
NAMESPACE_POOL_SIZE = 200
NAMESPACE_CLUSTER = "loadtest-cluster"

# Far above what ~150 concurrently ordering users could hold at once: the test is about DB/SSE
# behavior under load, not quota enforcement.
QUOTA = {
    "max_cpus": 9999,
    "max_memory_gb": 9999,
    "max_ssd_gb": 9999,
    "max_hdd_gb": 9999,
}

JSON = {"Accept": "application/json"}


class Report:
    """Collects failed creates and prints a created/existing summary per kind of item."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def fail(self, what: str, resp: httpx.Response) -> None:
        detail = resp.headers.get("HX-Retarget") or resp.text[:200]
        message = f"FAILED {what}: HTTP {resp.status_code} {detail}"
        print(message, file=sys.stderr)
        self.failures.append(message)

    def summary(self, kind: str, created: int, total: int) -> None:
        print(f"{kind}: {created} created, {total - created} already existed")


def login(client: httpx.Client) -> None:
    """Log in as admin, then carry the session in an explicit Cookie header: the server marks
    session_id Secure, which a client would otherwise not send back over plain-http localhost."""
    resp = client.post(
        "/auth/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    session_id = session_cookie(resp)
    if resp.status_code != 302 or session_id is None:
        sys.exit(
            f"admin login failed: HTTP {resp.status_code}; check ADMIN_USERNAME/ADMIN_PASSWORD"
        )
    client.headers["Cookie"] = f"session_id={session_id}"


def session_cookie(resp: httpx.Response) -> str | None:
    for header in resp.headers.get_list("set-cookie"):
        morsel = SimpleCookie(header).get("session_id")
        if morsel is not None and morsel.value:
            return morsel.value
    return None


def existing(client: httpx.Client, path: str, key: str = "name") -> dict[str, str]:
    resp = client.get(path)
    if resp.status_code != 200:
        sys.exit(f"GET {path} failed: HTTP {resp.status_code} {resp.text[:200]}")
    return {item[key]: item["id"] for item in resp.json()}


def ensure_catalog(client: httpx.Client, report: Report) -> None:
    created = 0
    if IMAGE_NAME not in existing(client, "/api/images"):
        resp = client.post(
            "/api/images",
            json={
                "name": IMAGE_NAME,
                "vapp_template_id": "urn:vcloud:vapptemplate:loadtest",
            },
        )
        if resp.status_code == 201:
            created += 1
        else:
            report.fail(f"VM image {IMAGE_NAME}", resp)
    if HW_CONFIG_NAME not in existing(client, "/api/hardware"):
        resp = client.post(
            "/api/hardware",
            json={
                "name": HW_CONFIG_NAME,
                "cpus": 1,
                "memory_mb": 1024,
                "disk_mb": 10240,
            },
        )
        if resp.status_code == 201:
            created += 1
        else:
            report.fail(f"hardware config {HW_CONFIG_NAME}", resp)
    report.summary("catalog entries", created, 2)


def form_created(resp: httpx.Response) -> bool:
    """The admin catalog forms answer 200 even on error; only HX-Retarget tells them apart."""
    return resp.status_code == 200 and "HX-Retarget" not in resp.headers


def ensure_pool(client: httpx.Client, report: Report) -> None:
    have = existing(client, "/api/static-vms")
    created = 0
    for i in range(1, STATIC_VM_POOL_SIZE + 1):
        name = f"loadtest-svm-{i:04d}"
        if name in have:
            continue
        resp = client.post(
            "/admin/catalog/static-vms",
            data={
                "name": name,
                "host": f"10.99.{i // 250 + 1}.{i % 250 + 1}",
                "username": "root",
                "password": STATIC_VM_PASSWORD,
                "cpus": "1",
                "memory_gb": "1",
            },
        )
        if form_created(resp):
            created += 1
        else:
            report.fail(f"static VM {name}", resp)
    report.summary("static VM pool", created, STATIC_VM_POOL_SIZE)

    have = existing(client, "/api/namespaces")
    created = 0
    for i in range(1, NAMESPACE_POOL_SIZE + 1):
        name = f"loadtest-ns-{i:04d}"
        if name in have:
            continue
        resp = client.post(
            "/admin/catalog/namespaces",
            data={"name": name, "cluster_name": NAMESPACE_CLUSTER},
        )
        if form_created(resp):
            created += 1
        else:
            report.fail(f"namespace {name}", resp)
    report.summary("namespace pool", created, NAMESPACE_POOL_SIZE)


def ensure_accounts(client: httpx.Client, report: Report) -> None:
    # A duplicate POST /api/users is a 500, not a 409 — never send one.
    have = existing(client, "/api/users", key="username")
    created = 0
    for i in range(1, N_ACCOUNTS + 1):
        username = f"{ACCOUNT_PREFIX}{i:04d}"
        user_id = have.get(username)
        if user_id is None:
            resp = client.post(
                "/api/users",
                json={
                    "username": username,
                    "password": ACCOUNT_PASSWORD,
                    "role": "user",
                },
            )
            if resp.status_code != 201:
                report.fail(f"account {username}", resp)
                continue
            user_id = resp.json()["id"]
            created += 1
        resp = client.patch(f"/api/users/{user_id}/quota", json=QUOTA)
        if resp.status_code != 200:
            report.fail(f"quota for {username}", resp)
        if i % 100 == 0:
            print(f"...{i}/{N_ACCOUNTS} accounts processed ({created} created so far)")
    report.summary("accounts", created, N_ACCOUNTS)


def main() -> None:
    try:
        check_target(BASE, os.environ.get(ALLOW_REMOTE_HOST_ENV))
    except TargetRefused as exc:
        sys.exit(f"refusing to seed: {exc}")

    report = Report()
    with httpx.Client(base_url=BASE, headers=JSON, timeout=30.0) as client:
        login(client)
        ensure_catalog(client, report)
        ensure_pool(client, report)
        ensure_accounts(client, report)
    if report.failures:
        sys.exit(f"seed finished with {len(report.failures)} failure(s)")
    print("seed complete")


if __name__ == "__main__":
    main()
