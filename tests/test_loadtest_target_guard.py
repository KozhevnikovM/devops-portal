"""loadtest/target_guard.py (#505): the load tooling refuses non-loopback and non-stub targets.

The module is loaded by file path — loadtest/ is not a package and the fast suite doesn't install
Locust — so these tests exercise exactly the file the seed script and locustfile import.
"""

import importlib.util
import socket
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "loadtest" / "target_guard.py"
_spec = importlib.util.spec_from_file_location("loadtest_target_guard", _PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class _Fetcher:
    """A fake health fetcher that records whether it was called."""

    def __init__(self, status=200, body=None, error=None):
        self.status = status
        self.body = {"status": "ok", "stub_terraform": True} if body is None else body
        self.error = error
        self.calls = []

    def __call__(self, base_url):
        self.calls.append(base_url)
        if self.error is not None:
            raise self.error
        return self.status, self.body


@pytest.fixture
def no_dns(monkeypatch):
    """Any name resolution fails the test: the host check must never consult DNS."""

    def _fail(*args, **kwargs):
        raise AssertionError("target guard must not resolve hostnames")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    monkeypatch.setattr(socket, "gethostbyname", _fail)
    monkeypatch.setattr(socket, "gethostbyname_ex", _fail)


# ── Host check ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://LOCALHOST:8000/",
        "http://127.0.0.1:8000",
        "http://127.5.6.7",
        "http://[::1]:8000",
    ],
)
def test_loopback_target_accepted(url, no_dns):
    fetcher = _Fetcher()
    guard.check_target(url, None, fetcher)
    assert fetcher.calls == [url]


def test_remote_target_refused_and_named(no_dns):
    fetcher = _Fetcher()
    with pytest.raises(guard.TargetRefused) as exc:
        guard.check_target("https://portal.example.com", None, fetcher)
    assert "portal.example.com" in str(exc.value)
    assert guard.ALLOW_REMOTE_HOST_ENV in str(exc.value)
    assert fetcher.calls == []


def test_name_resolving_to_loopback_refused_without_dns(no_dns):
    fetcher = _Fetcher()
    with pytest.raises(guard.TargetRefused):
        guard.check_target("http://host.docker.internal:8000", None, fetcher)
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "url", ["http://128.0.0.1", "http://10.0.0.1", "http://[::2]", "http://0.0.0.0"]
)
def test_non_loopback_addresses_refused(url, no_dns):
    with pytest.raises(guard.TargetRefused):
        guard.check_target(url, None, _Fetcher())


def test_override_for_a_different_host_refused(no_dns):
    fetcher = _Fetcher()
    with pytest.raises(guard.TargetRefused):
        guard.check_target("https://portal.example.com", "other.example.com", fetcher)
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "override",
    [
        "portal.example.com",
        "PORTAL.Example.COM",
        "portal.example.com:9999",
    ],
)
def test_matching_override_accepted_case_insensitive_port_ignored(override, no_dns):
    fetcher = _Fetcher()
    guard.check_target("https://Portal.Example.com:8443", override, fetcher)
    assert fetcher.calls == ["https://Portal.Example.com:8443"]


@pytest.mark.parametrize("override", ["", "   ", None])
def test_empty_override_is_no_override(override, no_dns):
    with pytest.raises(guard.TargetRefused):
        guard.check_target("https://portal.example.com", override, _Fetcher())


@pytest.mark.parametrize("url", ["ftp://localhost", "localhost:8000", "http://"])
def test_non_http_url_refused(url):
    with pytest.raises(guard.TargetRefused):
        guard.check_target(url, None, _Fetcher())


# ── Stub check ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fetcher",
    [
        _Fetcher(body={"status": "ok", "stub_terraform": False}),
        _Fetcher(body={"status": "ok"}),  # older server without the field
        _Fetcher(
            body={"status": "ok", "stub_terraform": "true"}
        ),  # only a JSON true counts
        _Fetcher(body=[]),
        _Fetcher(status=503, body={"status": "ok", "stub_terraform": True}),
        _Fetcher(error=ConnectionRefusedError("connection refused")),
        _Fetcher(error=TimeoutError("timed out")),
    ],
    ids=[
        "false",
        "missing",
        "string",
        "not-an-object",
        "non-200",
        "unreachable",
        "timeout",
    ],
)
def test_target_not_reporting_stub_mode_refused(fetcher):
    with pytest.raises(guard.TargetRefused):
        guard.check_target("http://localhost:8000", None, fetcher)


def test_not_json_body_refused():
    with pytest.raises(guard.TargetRefused):
        guard.check_target("http://localhost:8000", None, lambda url: (200, None))


def test_override_does_not_bypass_stub_check(no_dns):
    fetcher = _Fetcher(body={"status": "ok", "stub_terraform": False})
    with pytest.raises(guard.TargetRefused) as exc:
        guard.check_target("https://portal.example.com", "portal.example.com", fetcher)
    assert "stub_terraform" in str(exc.value)
    assert fetcher.calls == ["https://portal.example.com"]


# ── Default fetcher ───────────────────────────────────────────────────────────


def test_default_fetcher_uses_a_5s_timeout(monkeypatch):
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"status": "ok", "stub_terraform": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return _Resp()

    monkeypatch.setattr(guard.urllib.request, "urlopen", _urlopen)
    assert guard.fetch_health("http://localhost:8000/") == (
        200,
        {"status": "ok", "stub_terraform": True},
    )
    assert seen == {"url": "http://localhost:8000/health", "timeout": 5}


def test_default_fetcher_unreachable_target_refused():
    # Port 9 (discard) on loopback is closed on any dev/CI host: a real connection refusal.
    with pytest.raises(guard.TargetRefused):
        guard.check_target("http://127.0.0.1:9", None)
