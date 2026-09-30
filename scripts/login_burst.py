"""Reproducible login-burst measurement against a running portal (issue #493).

Seeds N test users (idempotent), then fires rounds x users concurrent
`POST /auth/login` requests with at most --concurrency in flight. While the burst
runs, a probe does `GET /auth/login` every --probe-interval seconds to show whether
unrelated requests are still being served. Prints concurrency, total and failed
logins, error rate, p50/p95 latency for logins and the probe, and the number of
SQLAlchemy `QueuePool` timeouts the app logged *during this run only*
(`docker compose logs --since <run start>`).

Run the same command against the same stack before and after a change so the
numbers are comparable:

    python scripts/login_burst.py --base-url http://localhost:8000 \\
        --users 100 --rounds 5 --concurrency 100

Admin credentials for seeding come from --admin-user / --admin-password, or the
ADMIN_USERNAME / ADMIN_PASSWORD environment variables (default admin / changeme).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

USER_PREFIX = "burst-user-"
USER_PASSWORD = "burst-password-493"


@dataclass
class Samples:
    latencies: list[float] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)

    def ok(self, seconds: float) -> None:
        self.latencies.append(seconds)

    def fail(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    @property
    def total(self) -> int:
        return len(self.latencies) + sum(self.failures.values())

    @property
    def failed(self) -> int:
        return sum(self.failures.values())


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]


def fmt_ms(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds * 1000:.0f} ms"


async def seed_users(client: httpx.AsyncClient, count: int, admin_user: str, admin_password: str) -> None:
    resp = await client.post("/auth/login", data={"username": admin_user, "password": admin_password})
    if resp.status_code != 302 or "session_id" not in resp.cookies:
        sys.exit(f"admin login failed ({resp.status_code}); check --admin-user/--admin-password")
    cookies = {"session_id": resp.cookies["session_id"]}

    existing = await client.get("/api/users", cookies=cookies)
    existing.raise_for_status()
    have = {u["username"] for u in existing.json()}
    missing = [f"{USER_PREFIX}{i}" for i in range(count) if f"{USER_PREFIX}{i}" not in have]
    for name in missing:
        r = await client.post(
            "/api/users",
            json={"username": name, "password": USER_PASSWORD, "role": "user"},
            cookies=cookies,
        )
        r.raise_for_status()
    print(f"seeded {len(missing)} new user(s); {count - len(missing)} already present")


async def login_once(client: httpx.AsyncClient, username: str, samples: Samples) -> None:
    start = time.perf_counter()
    try:
        resp = await client.post("/auth/login", data={"username": username, "password": USER_PASSWORD})
    except httpx.TimeoutException:
        samples.fail("client timeout")
        return
    except httpx.HTTPError as exc:
        samples.fail(type(exc).__name__)
        return
    if resp.status_code == 302 and "session_id" in resp.cookies:
        samples.ok(time.perf_counter() - start)
    else:
        samples.fail(f"HTTP {resp.status_code}")


async def probe(client: httpx.AsyncClient, interval: float, stop: asyncio.Event, samples: Samples) -> None:
    while not stop.is_set():
        start = time.perf_counter()
        try:
            resp = await client.get("/auth/login")
            if resp.status_code == 200:
                samples.ok(time.perf_counter() - start)
            else:
                samples.fail(f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            samples.fail(type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def count_pool_timeouts(since: str, compose_cmd: list[str], service: str) -> int | None:
    try:
        out = subprocess.run(
            [*compose_cmd, "logs", "--no-color", "--since", since, service],
            capture_output=True, text=True, timeout=60, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warning: could not read container logs ({exc}); pool timeouts not counted", file=sys.stderr)
        return None
    # Each timeout's traceback prints the exception twice: once nested inside an
    # ExceptionGroup ("    | sqlalchemy...") and once at top level. Count top-level
    # lines only, so the number is one per timed-out request.
    count = 0
    for line in out.stdout.splitlines():
        message = line.split("| ", 1)[1] if "| " in line else line  # drop "app-1  | "
        if message.startswith("sqlalchemy.exc.TimeoutError: QueuePool limit"):
            count += 1
    return count


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=100, help="distinct test accounts (seeded if missing)")
    parser.add_argument("--rounds", type=int, default=5, help="logins per account")
    parser.add_argument("--concurrency", type=int, default=100, help="max logins in flight")
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument("--probe-interval", type=float, default=0.2)
    parser.add_argument("--admin-user", default=os.environ.get("ADMIN_USERNAME", "admin"))
    parser.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", "changeme"))
    parser.add_argument("--compose-cmd", default="docker compose", help="command used to read app logs")
    parser.add_argument("--compose-service", default="app")
    parser.add_argument("--no-logs", action="store_true", help="skip the QueuePool log count")
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=args.concurrency + 1, max_keepalive_connections=args.concurrency + 1)
    timeout = httpx.Timeout(args.request_timeout)
    async with httpx.AsyncClient(base_url=args.base_url, limits=limits, timeout=timeout) as client:
        await seed_users(client, args.users, args.admin_user, args.admin_password)

        # Everything the app logs from here on belongs to this run.
        run_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        logins, probes = Samples(), Samples()
        sem = asyncio.Semaphore(args.concurrency)

        async def bounded(username: str) -> None:
            async with sem:
                await login_once(client, username, logins)

        stop = asyncio.Event()
        probe_task = asyncio.create_task(probe(client, args.probe_interval, stop, probes))
        wall_start = time.perf_counter()
        await asyncio.gather(*(
            bounded(f"{USER_PREFIX}{i}") for _ in range(args.rounds) for i in range(args.users)
        ))
        wall = time.perf_counter() - wall_start
        stop.set()
        await probe_task

    pool_timeouts = None if args.no_logs else count_pool_timeouts(
        run_start, args.compose_cmd.split(), args.compose_service,
    )

    print(f"run start (UTC):      {run_start}")
    print(f"concurrency:          {args.concurrency} in flight, {args.users} users x {args.rounds} rounds")
    print(f"wall time:            {wall:.1f} s ({logins.total / wall:.1f} logins/s)")
    print(f"logins total/failed:  {logins.total} / {logins.failed}")
    print(f"login error rate:     {100 * logins.failed / max(logins.total, 1):.1f}%")
    print(f"login p50 / p95:      {fmt_ms(percentile(logins.latencies, 50))} / "
          f"{fmt_ms(percentile(logins.latencies, 95))}")
    if logins.failures:
        print(f"login failures:       {logins.failures}")
    print(f"probe total/failed:   {probes.total} / {probes.failed}")
    print(f"probe p50 / p95:      {fmt_ms(percentile(probes.latencies, 50))} / "
          f"{fmt_ms(percentile(probes.latencies, 95))}")
    print(f"QueuePool timeouts:   {'n/a' if pool_timeouts is None else pool_timeouts} (this run only)")


if __name__ == "__main__":
    asyncio.run(main())
