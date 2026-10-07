"""Target safety guard shared by loadtest/seed.py and loadtest/locustfile.py (#505).

Both tools refuse to send a single state-changing request unless:

1. the target host is loopback — the literal name `localhost`, an IPv4 address in 127.0.0.0/8,
   or `::1` — or the operator named that exact host in LOADTEST_ALLOW_REMOTE_HOST (compared
   case-insensitively, port ignored). There is no DNS lookup: a name like `host.docker.internal`
   is refused unless named explicitly, whatever it resolves to.
2. the target's unauthenticated `GET /health` answers 200 with `"stub_terraform": true`, so VM
   orders can't reach real Terraform/vCloud Director. This check has no override.

Standard library only, so the fast pytest suite can unit-test it without Locust or httpx.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

ALLOW_REMOTE_HOST_ENV = "LOADTEST_ALLOW_REMOTE_HOST"
HEALTH_TIMEOUT_SECONDS = 5


class TargetRefused(Exception):
    """The target failed the host check or the stub-mode check."""


# A health fetcher takes the base URL and returns (status code, parsed JSON body or None).
# Any exception it raises (unreachable target, timeout, malformed response) means refusal.
HealthFetcher = Callable[[str], tuple[int, Any]]


def fetch_health(base_url: str) -> tuple[int, Any]:
    """`GET <base_url>/health` with a 5 s timeout; the body is None when it isn't JSON."""
    url = base_url.rstrip("/") + "/health"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        # The scheme is the operator's own target URL, already restricted to http(s) by check_target.
        with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT_SECONDS) as resp:  # nosec B310
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def is_loopback_host(host: str) -> bool:
    """Literal loopback only: `localhost`, 127.0.0.0/8 or ::1 — never resolved through DNS."""
    host = host.lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 4:
        return address in ipaddress.ip_network("127.0.0.0/8")
    return address == ipaddress.ip_address("::1")


def _override_host(allow_remote_host: str | None) -> str | None:
    """The override's host, lower-cased, with any port (and brackets around IPv6) removed."""
    value = (allow_remote_host or "").strip()
    if not value:
        return None
    if "//" not in value:
        value = "//" + value
    return (urlsplit(value).hostname or "").lower() or None


def check_target(
    base_url: str,
    allow_remote_host: str | None,
    health_fetcher: HealthFetcher = fetch_health,
) -> None:
    """Raise TargetRefused unless `base_url` passes the host check and reports stub mode."""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise TargetRefused(
            f"load-test target {base_url!r} is not an http(s) URL with a host"
        )

    if not is_loopback_host(host) and _override_host(allow_remote_host) != host:
        raise TargetRefused(
            f"load-test target host {host!r} ({base_url}) is not loopback (localhost, 127.0.0.0/8, ::1). "
            f"To load-test it anyway, set {ALLOW_REMOTE_HOST_ENV}={host} — the target must still "
            f"report stub_terraform: true on GET /health."
        )

    try:
        status, body = health_fetcher(base_url)
    except (
        Exception
    ) as exc:  # fail closed: whatever went wrong, the target is unverified
        raise TargetRefused(
            f"load-test target {base_url}: GET /health failed ({exc}); refusing"
        ) from exc
    if status != 200:
        raise TargetRefused(
            f"load-test target {base_url}: GET /health returned {status}; refusing"
        )
    if not isinstance(body, dict) or body.get("stub_terraform") is not True:
        raise TargetRefused(
            f"load-test target {base_url} does not report stub_terraform: true on GET /health — "
            f"VM orders could reach real infrastructure. This check has no override; run the "
            f"target with USE_STUB_TERRAFORM=true."
        )
