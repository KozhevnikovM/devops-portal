#!/usr/bin/env python3
"""Check that a running portal serves every script and stylesheet its pages reference (#542).

Used by the Docker smoke test (scripts/smoke_test_docker.sh and .github/workflows/
docker-smoke-test.yml) against the production image. Signs in as the seeded admin, fetches pages
built on base.html (/environments, /book/vm) plus the standalone login page, requests every
same-origin <script src> and <link rel="stylesheet" href> they reference, and checks that
GET /health/ready reports `static_assets` ok.

    check_page_assets.py --base-url http://127.0.0.1:8000 --password smoke-test-admin

With --expect-missing PATH it checks the opposite: that the asset check and readiness both fail
and name PATH (the smoke test's negative step, after deleting a script from the container).
Standard library only, so it runs on a bare CI runner.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

PAGES = ("/environments", "/book/vm", "/auth/login")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class _AssetRefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.refs.append(attrs["src"])
        elif (
            tag == "link"
            and "stylesheet" in (attrs.get("rel") or "").split()
            and attrs.get("href")
        ):
            self.refs.append(attrs["href"])


def _request(base_url, path, *, cookie=None, data=None, origin=None):
    """(status, headers, body) without following redirects or raising on HTTP errors."""
    request = urllib.request.Request(urllib.parse.urljoin(base_url, path), data=data)
    if cookie:
        request.add_header("Cookie", cookie)
    if origin:
        request.add_header("Origin", origin)
    try:
        # The base URL is validated as http(s) in main(), so no file:// or custom schemes.
        with _OPENER.open(request, timeout=10) as response:  # nosec B310
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def _login(base_url, origin, username, password):
    form = urllib.parse.urlencode({"username": username, "password": password}).encode()
    status, headers, _ = _request(base_url, "/auth/login", data=form, origin=origin)
    for header in headers.get_all("Set-Cookie") or []:
        name, _, rest = header.partition("=")
        if name.strip() == "session_id":
            return f"session_id={rest.split(';', 1)[0]}"
    sys.exit(f"login as {username!r} failed (HTTP {status}, no session cookie)")


def _missing_assets(base_url, cookie):
    """Same-origin assets referenced by PAGES that are not served with 200, as sorted paths."""
    host = urllib.parse.urlsplit(base_url).netloc
    references = set()
    for page in PAGES:
        status, _, body = _request(base_url, page, cookie=cookie)
        if status != 200:
            sys.exit(f"GET {page} returned HTTP {status}; expected the rendered page")
        parser = _AssetRefs()
        parser.feed(body.decode("utf-8", "replace"))
        for ref in parser.refs:
            url = urllib.parse.urlsplit(urllib.parse.urljoin(base_url + page, ref))
            if url.netloc == host:
                references.add(url.path + (f"?{url.query}" if url.query else ""))
    print(f"checked pages {', '.join(PAGES)}: {len(references)} same-origin assets")
    missing = []
    for ref in sorted(references):
        status, _, _ = _request(base_url, ref)
        print(f"  {status} {ref}")
        if status != 200:
            missing.append(urllib.parse.urlsplit(ref).path)
    return missing


def _readiness(base_url):
    status, _, body = _request(base_url, "/health/ready")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {"raw": body.decode("utf-8", "replace")}
    print(f"GET /health/ready -> HTTP {status}: {payload}")
    return status, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--origin", default="http://localhost:8000", help="the app's BASE_URL"
    )
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--expect-missing", metavar="PATH")
    args = parser.parse_args(argv)
    if urllib.parse.urlsplit(args.base_url).scheme not in ("http", "https"):
        sys.exit("--base-url must be http(s)")
    base_url = args.base_url.rstrip("/")

    cookie = _login(base_url, args.origin, args.username, args.password)
    missing = _missing_assets(base_url, cookie)
    status, payload = _readiness(base_url)

    if args.expect_missing:
        expected = args.expect_missing
        problems = []
        if expected not in missing:
            problems.append(
                f"asset check did not report {expected} missing (missing: {missing})"
            )
        if status != 503 or expected not in payload.get("missing_static_assets", []):
            problems.append(f"readiness did not fail naming {expected}")
        if problems:
            sys.exit("negative check FAILED: " + "; ".join(problems))
        print(f"negative check passed: {expected} is reported missing by both checks")
        return

    if missing:
        sys.exit(f"assets referenced by the pages are not served: {', '.join(missing)}")
    if status != 200 or payload.get("static_assets") != "ok":
        sys.exit("readiness does not report static_assets ok")
    print("all referenced assets are served and readiness reports static_assets ok")


if __name__ == "__main__":
    main()
