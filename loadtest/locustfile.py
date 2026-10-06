"""Load test for the portal's open tabs and booking flow (#505, see loadtest/README.md).

It doubles as a regression guard for #407: SSE connections held open for a tab's lifetime once
exhausted the DB pool. Two kinds of user, each logged in as a distinct seeded account:

- PassiveWatcher (weight 85): holds one GET /events/stream open, and keeps a tab on the VM
  bookings page or the environments page — alternating the "mine" and "all" lists — replaying
  that page's 60 s reconcile poll exactly as the browser would (loadtest/reconcile.py).
- ActiveOrderer (weight 15): also holds an SSE connection and keeps its own "mine" bookings tab
  polling in the background, while it loops order -> poll until settled -> hold -> release,
  mostly VMs (stub Terraform) and sometimes a pooled static VM or namespace.

Before any user starts, the target guard (loadtest/target_guard.py) must accept the host: loopback
(or named in LOADTEST_ALLOW_REMOTE_HOST) and reporting stub_terraform: true. Otherwise the run
quits with exit code 1. Requires loadtest/seed.py to have been run against the same stack.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path

import gevent
from locust import HttpUser, between, events, task
from locust.exception import StopUser
from locust.runners import WorkerRunner

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reconcile import ReconcileMarkupError, Section, parse_section
from target_guard import (
    ALLOW_REMOTE_HOST_ENV,
    TargetRefused,
    check_target,
)

logger = logging.getLogger(__name__)

N_ACCOUNTS = 1000
ACCOUNT_PREFIX = "loadtest-"
# A fixed test-only credential: the guard only lets this run against a stub stack.
ACCOUNT_PASSWORD = "loadtest-pass-1234"  # nosec B105

IMAGE_NAME = "loadtest-image"
HW_CONFIG_NAME = "loadtest-small"

JSON = {"Accept": "application/json"}
TRANSIENT = {"PENDING", "PROVISIONING", "CONFIGURING", "RETRY"}
RECONCILE_INTERVAL_SECONDS = 60
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 60

# Every simulated user takes a distinct seeded account, shuffled once per process.
_ACCOUNT_POOL = random.sample(range(1, N_ACCOUNTS + 1), N_ACCOUNTS)
_next_account = 0

# Hosts the guard accepted for this run; users refuse to start against any other.
_approved_hosts: set[str] = set()


def _claim_account() -> str:
    global _next_account
    idx = _ACCOUNT_POOL[_next_account % N_ACCOUNTS]
    _next_account += 1
    return f"{ACCOUNT_PREFIX}{idx:04d}"


# ── Target guard (D3) ─────────────────────────────────────────────────────────


def _guard(host: str | None) -> None:
    check_target(host or "", os.environ.get(ALLOW_REMOTE_HOST_ENV))
    _approved_hosts.add(host or "")


@events.init.add_listener
def _guard_on_init(environment, **_kwargs):
    """A headless run with --host fails before the runner spawns anyone."""
    if isinstance(environment.runner, WorkerRunner) or not environment.host:
        return
    try:
        _guard(environment.host)
    except TargetRefused as exc:
        logger.error("refusing to load-test: %s", exc)
        environment.process_exit_code = 1
        sys.exit(1)  # SystemExit is not swallowed by Locust's event hooks


@events.test_start.add_listener
def _guard_on_test_start(environment, **_kwargs):
    """Every start, including the web UI, where the host can change per run."""
    if isinstance(environment.runner, WorkerRunner):
        return
    try:
        _guard(environment.host)
    except TargetRefused as exc:
        logger.error("refusing to load-test: %s", exc)
        environment.process_exit_code = 1
        environment.runner.quit()


# ── Users ─────────────────────────────────────────────────────────────────────


class _PortalUser(HttpUser):
    """Logs in as one seeded account and holds /events/stream open on a background greenlet for its
    whole lifetime, like a browser tab that stays open."""

    abstract = True

    def on_start(self):
        if self.host not in _approved_hosts:
            raise StopUser()  # the guard did not accept this host; never send a request
        self._greenlets: list[gevent.Greenlet] = []
        self._login()
        self._greenlets.append(gevent.spawn(self._hold_sse_connection))

    def on_stop(self):
        for greenlet in getattr(self, "_greenlets", []):
            greenlet.kill(block=False)

    def _login(self):
        username = _claim_account()
        with self.client.post(
            "/auth/login",
            data={"username": username, "password": ACCOUNT_PASSWORD},
            allow_redirects=False,
            name="/auth/login",
            catch_response=True,
        ) as resp:
            session_id = _session_cookie(resp)
            if resp.status_code != 302 or session_id is None:
                resp.failure(f"login failed for {username}: HTTP {resp.status_code}")
                raise StopUser()
            resp.success()
        # session_id is Secure; the client won't send it back over plain-http localhost (D4).
        self.client.headers["Cookie"] = f"session_id={session_id}"

    def _hold_sse_connection(self):
        while True:
            with self.client.get(
                "/events/stream",
                stream=True,
                timeout=None,
                headers={"Accept": "text/event-stream"},
                name="/events/stream [held open]",
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"unexpected status {resp.status_code}")
                else:
                    resp.success()
            # Leaving the block reports the connect now, not when the stream ends; the open
            # response is consumed afterwards.
            if resp.status_code == 200:
                try:
                    for _ in resp.iter_lines():
                        pass  # a tab acts on few of these; what matters is holding it open
                except gevent.GreenletExit:
                    raise
                except Exception:
                    logger.debug("SSE connection dropped", exc_info=True)
                finally:
                    resp.close()
            # EventSource reconnects after a short delay.
            gevent.sleep(random.uniform(3, 6))

    # A tab: load a list page, then replay its reconcile poll every 60 s for a while (D7).

    def _view_page(self, path: str, filter_: str, polls: int) -> None:
        section = self._load_page(path, filter_)
        for _ in range(polls):
            gevent.sleep(RECONCILE_INTERVAL_SECONDS)
            if section is None:
                return
            self._reconcile(section)

    def _load_page(self, path: str, filter_: str) -> Section | None:
        with self.client.get(
            path,
            params={"filter": filter_},
            allow_redirects=False,
            name=f"{path} [page, filter={filter_}]",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return None
            try:
                section = parse_section(resp.text)
            except ReconcileMarkupError as exc:
                resp.failure(f"template drift: {exc}")
                return None
            resp.success()
            return section

    def _reconcile(self, section: Section) -> None:
        with self.client.get(
            section.next_url(),
            allow_redirects=False,
            name=section.stats_name,
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            section.apply_response(resp.text)
            resp.success()


def _session_cookie(resp) -> str | None:
    value = resp.cookies.get("session_id")
    if value:
        return value
    for header in (
        resp.raw.headers.getlist("Set-Cookie") if resp.raw is not None else []
    ):
        morsel = SimpleCookie(header).get("session_id")
        if morsel is not None and morsel.value:
            return morsel.value
    return None


class PassiveWatcher(_PortalUser):
    weight = 85
    wait_time = between(0, 5)

    _PAGES = (
        ("/", "mine"),
        ("/", "all"),
        ("/environments", "mine"),
        ("/environments", "all"),
    )

    def on_start(self):
        super().on_start()
        self._page_index = random.randrange(len(self._PAGES))

    @task
    def keep_tab_open(self):
        # Watchers own no bookings: "mine" exercises the empty-batch and probe path, "all" the
        # batch read over other users' rows and list-visibility authorization.
        path, filter_ = self._PAGES[self._page_index % len(self._PAGES)]
        self._page_index += 1
        self._view_page(path, filter_, polls=random.randint(1, 5))


class ActiveOrderer(_PortalUser):
    weight = 15
    wait_time = between(5, 20)

    def on_start(self):
        super().on_start()
        self._greenlets.append(gevent.spawn(self._own_bookings_tab))

    def _own_bookings_tab(self):
        while True:
            self._view_page("/", "mine", polls=random.randint(1, 5))

    @task(3)
    def order_vm(self):
        self._order_and_release(
            "VM", {"image_name": IMAGE_NAME, "hw_config_name": HW_CONFIG_NAME}
        )

    @task(1)
    def order_pooled(self):
        self._order_and_release(random.choice(["STATIC_VM", "NAMESPACE"]), {})

    def _order_and_release(self, resource_type: str, extra: dict) -> None:
        label = f"lt-{uuid.uuid4().hex}"
        body = {
            "resource_type": resource_type,
            "ttl_minutes": 30,
            "label": label,
            **extra,
        }
        with self.client.post(
            "/api/bookings",
            json=body,
            headers=JSON,
            name=f"/api/bookings [{resource_type}]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 409 and resource_type != "VM":
                resp.success()  # pool unavailable: nothing of ours to release
                return
            if resp.status_code != 201:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return
            resp.success()
            booking = resp.json()
        status = booking["status"]
        if status == "QUEUED":
            return  # pool empty: TTL or promotion cleans it up

        status = self._wait_until_settled(booking["id"], label, status)
        if status is None:
            return
        # Hold it like a real user before releasing.
        gevent.sleep(random.uniform(10, 60))
        if status in ("READY", "FAILED"):
            with self.client.delete(
                f"/api/bookings/{booking['id']}",
                headers=JSON,
                name="/api/bookings/[id] [release]",
                catch_response=True,
            ) as resp:
                if resp.status_code == 202:
                    resp.success()
                else:
                    resp.failure(f"HTTP {resp.status_code}: {resp.text[:200]}")

    def _wait_until_settled(
        self, booking_id: str, label: str, status: str
    ) -> str | None:
        """Poll by unique label (there is no GET /api/bookings/{id}) until the status leaves the
        transient set, for up to 60 s. None when the booking vanished or never settled."""
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while status in TRANSIENT:
            gevent.sleep(POLL_INTERVAL_SECONDS)
            with self.client.get(
                "/api/bookings",
                params={"label": label},
                headers=JSON,
                name="/api/bookings?label= [poll]",
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"HTTP {resp.status_code}")
                    return None
                match = next((b for b in resp.json() if b["id"] == booking_id), None)
                if match is None:
                    resp.failure(f"booking {booking_id} missing from its label listing")
                    return None
                status = match["status"]
                if status in TRANSIENT and time.monotonic() >= deadline:
                    resp.failure(
                        f"booking {booking_id} still {status} after {POLL_TIMEOUT_SECONDS}s"
                    )
                    return None
                resp.success()
        return status
