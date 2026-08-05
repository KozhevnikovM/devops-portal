"""One-time setup for the 1000-user load test (see docs/features/load-testing-1000-users.md):
creates test accounts, generous quotas, and a minimal catalog (one VM image/hardware config,
plus a pooled static-VM/namespace set) via the admin API.

Idempotent — checks what already exists via the JSON list endpoints before creating anything,
so it's safe to re-run against an already-seeded stack (and avoids the app's create endpoints,
which don't return a clean 4xx on a duplicate name/username, only the HTML admin forms do).

Run: python loadtest/seed.py   (against a local `docker compose up`, USE_STUB_TERRAFORM=true)
"""
import os
import sys

import httpx

BASE = os.environ.get("PORTAL_URL", "http://localhost:8000")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

N_ACCOUNTS = 1000
ACCOUNT_PREFIX = "loadtest-"
ACCOUNT_PASSWORD = "loadtest-pass-1234"  # fixed test-only credential, local stub stack

IMAGE_NAME = "loadtest-image"
HW_CONFIG_NAME = "loadtest-small"

STATIC_VM_POOL_SIZE = 200
NAMESPACE_POOL_SIZE = 200
NAMESPACE_CLUSTER = "loadtest-cluster"

# Comfortably above what 150 concurrently-active simulated users could plausibly hold at once —
# the point of this test is DB/SSE behavior under load, not quota enforcement.
QUOTA = {"max_cpus": 9999, "max_memory_gb": 9999, "max_ssd_gb": 9999, "max_hdd_gb": 9999}


def login(client: httpx.Client) -> None:
    resp = client.post(f"{BASE}/auth/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    if resp.status_code not in (302, 303):
        sys.exit(f"admin login failed: {resp.status_code} {resp.text[:300]}")


def ensure_catalog(client: httpx.Client) -> None:
    images = {img["name"] for img in client.get(f"{BASE}/api/images").json()}
    if IMAGE_NAME not in images:
        resp = client.post(
            f"{BASE}/api/images",
            json={"name": IMAGE_NAME, "vapp_template_id": "urn:vcloud:vapptemplate:loadtest"},
        )
        resp.raise_for_status()
        print(f"created VM image {IMAGE_NAME!r}")

    hw_configs = {hw["name"] for hw in client.get(f"{BASE}/api/hardware").json()}
    if HW_CONFIG_NAME not in hw_configs:
        resp = client.post(
            f"{BASE}/api/hardware",
            json={"name": HW_CONFIG_NAME, "cpus": 1, "memory_mb": 1024, "disk_mb": 10240},
        )
        resp.raise_for_status()
        print(f"created hardware config {HW_CONFIG_NAME!r}")


def ensure_pool(client: httpx.Client) -> None:
    existing_svms = {svm["name"] for svm in client.get(f"{BASE}/api/static-vms").json()}
    created = 0
    for i in range(1, STATIC_VM_POOL_SIZE + 1):
        name = f"loadtest-svm-{i:04d}"
        if name in existing_svms:
            continue
        resp = client.post(
            f"{BASE}/admin/catalog/static-vms",
            data={
                "name": name, "host": f"10.99.{(i // 250) + 1}.{i % 250 + 1}",
                "username": "root", "password": "loadtest",
            },
        )
        if resp.status_code != 200:
            print(f"warning: static-vm {name} -> {resp.status_code} {resp.text[:200]}")
        else:
            created += 1
    print(f"static VM pool: {created} created, {STATIC_VM_POOL_SIZE - created} already existed")

    existing_ns = {ns["name"] for ns in client.get(f"{BASE}/api/namespaces").json()}
    created = 0
    for i in range(1, NAMESPACE_POOL_SIZE + 1):
        name = f"loadtest-ns-{i:04d}"
        if name in existing_ns:
            continue
        resp = client.post(
            f"{BASE}/admin/catalog/namespaces",
            data={"name": name, "cluster_name": NAMESPACE_CLUSTER},
        )
        if resp.status_code != 200:
            print(f"warning: namespace {name} -> {resp.status_code} {resp.text[:200]}")
        else:
            created += 1
    print(f"namespace pool: {created} created, {NAMESPACE_POOL_SIZE - created} already existed")


def ensure_accounts(client: httpx.Client) -> None:
    existing = {u["username"]: u["id"] for u in client.get(f"{BASE}/api/users").json()}
    created = 0
    for i in range(1, N_ACCOUNTS + 1):
        username = f"{ACCOUNT_PREFIX}{i:04d}"
        user_id = existing.get(username)
        if user_id is None:
            resp = client.post(
                f"{BASE}/api/users",
                json={"username": username, "password": ACCOUNT_PASSWORD, "role": "user"},
            )
            resp.raise_for_status()
            user_id = resp.json()["id"]
            created += 1
        client.patch(f"{BASE}/api/users/{user_id}/quota", json=QUOTA)
        if i % 100 == 0:
            print(f"...{i}/{N_ACCOUNTS} accounts processed ({created} newly created so far)")
    print(f"accounts: {created} newly created, {N_ACCOUNTS - created} already existed")


def main() -> None:
    with httpx.Client(timeout=15.0) as client:
        login(client)
        ensure_catalog(client)
        ensure_pool(client)
        ensure_accounts(client)
    print("seed complete")


if __name__ == "__main__":
    main()
