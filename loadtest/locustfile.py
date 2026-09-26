"""Load test for the devops-portal booking flow + SSE (see
docs/features/load-testing-1000-users.md — this doubles as a regression guard for #407, the DB
connection pool exhaustion caused by SSE connections held open for a tab's whole lifetime).

Two user shapes, weighted to model real traffic:
- PassiveWatcher (weight 85, ~850 of 1000): logs in, holds one SSE connection open for the run's
  duration, occasionally reloads a page. Never orders anything — this is exactly the traffic
  shape that starved the DB pool before #407 was fixed, so it's the most important half of this
  test even though it looks like it "does nothing."
- ActiveOrderer (weight 15, ~150 of 1000): also holds an SSE connection, and loops
  order -> poll until non-transient -> hold -> release, mostly VM bookings (stub Terraform
  adapter) with some pooled static-VM/namespace bookings mixed in.

Requires loadtest/seed.py to have been run first (1000 accounts, catalog, and a pooled-resource
set it creates). Run: see loadtest/README.md.
"""
import logging
import random
import time
import uuid

import gevent
from locust import HttpUser, between, task

logger = logging.getLogger(__name__)

N_ACCOUNTS = 1000
ACCOUNT_PREFIX = "loadtest-"
ACCOUNT_PASSWORD = "loadtest-pass-1234"  # fixed test-only credential, local stub stack

IMAGE_NAME = "loadtest-image"
HW_CONFIG_NAME = "loadtest-small"

# Every simulated user picks a distinct seeded account, so no two Locust users fight over the
# same session — shuffle once per process so a run doesn't always hand out accounts in the same
# order regardless of --spawn-rate/--users.
_ACCOUNT_POOL = list(range(1, N_ACCOUNTS + 1))
random.shuffle(_ACCOUNT_POOL)
_next_account_idx = 0


def _claim_account() -> str:
    global _next_account_idx
    idx = _ACCOUNT_POOL[_next_account_idx % N_ACCOUNTS]
    _next_account_idx += 1
    return f"{ACCOUNT_PREFIX}{idx:04d}"


class _SSEConnectedUser(HttpUser):
    """Base class: logs in as one seeded account, then holds one /events/stream connection open
    on a background greenlet for the whole run — alongside whatever @task methods the subclass
    schedules — mirroring a real browser tab that stays open."""

    abstract = True

    def on_start(self):
        username = _claim_account()
        resp = self.client.post(
            "/auth/login",
            data={"username": username, "password": ACCOUNT_PASSWORD},
            name="/auth/login",
        )
        if resp.status_code not in (200, 302, 303):
            resp.failure(f"login failed for {username}: {resp.status_code}")
        self._sse_greenlet = gevent.spawn(self._hold_sse_connection)

    def on_stop(self):
        greenlet = getattr(self, "_sse_greenlet", None)
        if greenlet is not None:
            greenlet.kill(block=False)

    def _hold_sse_connection(self):
        # catch_response=True is required by Locust whenever the response is used as a
        # context manager — without it, exiting the `with` block raises a LocustError instead
        # of just recording the request normally.
        with self.client.get(
            "/events/stream", stream=True, timeout=None,
            name="/events/stream [held open]", catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"unexpected status {resp.status_code}")
                return
            resp.success()
            try:
                for _ in resp.iter_lines():
                    pass  # a real tab does nothing with most of these — just keep it open
            except gevent.GreenletExit:
                raise  # user stopped — let the kill from on_stop() propagate
            except Exception:
                logger.debug("SSE connection dropped", exc_info=True)


class PassiveWatcher(_SSEConnectedUser):
    weight = 85
    wait_time = between(45, 90)

    @task
    def browse(self):
        self.client.get("/", name="/ [passive browse]")


class ActiveOrderer(_SSEConnectedUser):
    weight = 15
    wait_time = between(5, 20)

    @task(3)
    def order_vm(self):
        self._order_and_release(
            "VM", {"image_name": IMAGE_NAME, "hw_config_name": HW_CONFIG_NAME},
        )

    @task(1)
    def order_pooled(self):
        self._order_and_release(random.choice(["STATIC_VM", "NAMESPACE"]), {})

    def _order_and_release(self, resource_type: str, extra: dict):
        label = f"loadtest-{uuid.uuid4().hex[:8]}"
        body = {"resource_type": resource_type, "ttl_minutes": 30, "label": label, **extra}
        resp = self.client.post("/api/bookings", json=body, name=f"/api/bookings [{resource_type}]")
        if resp.status_code not in (201, 202):
            return  # quota/pool exhaustion etc — already recorded as a Locust failure
        booking = resp.json()
        status = booking.get("status")
        if status == "QUEUED":
            return  # pool was empty — nothing of ours to release, it'll promote on its own

        deadline = time.time() + 60
        while status not in ("READY", "FAILED") and time.time() < deadline:
            gevent.sleep(2)
            listing = self.client.get(
                f"/api/bookings?label={label}", name="/api/bookings?label= [poll]",
            ).json()
            if not listing:
                return
            status = listing[0]["status"]

        gevent.sleep(random.uniform(10, 60))  # hold it like a real user would before releasing

        if status in ("READY", "FAILED"):
            self.client.delete(
                f"/api/bookings/{booking['id']}", name="/api/bookings/[id] [release]",
            )
