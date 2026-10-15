"""Readiness `static_assets` check (#542): every script/stylesheet the templates reference exists.

Evaluated live on each `/health/ready` call — a template edited while the app keeps running (the
dev bind mount) or an asset removed from the static volume shows up on the next call, without a
restart. Existence-only: changed content under an unchanged name is out of scope.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.static_assets import StaticAssetCheck

_BASE = """<!doctype html><head>
<link rel="stylesheet" href="/static/css/tailwind.css?v=2">
<script src="/static/js/htmx.min.js"></script>
<script src="/static/js/row_menu.js"></script>
<script src="{{ url_for('static', path='dynamic.js') }}"></script>
<a href="/static/docs/guide.pdf">not a script or stylesheet</a>
</head>"""


@pytest.fixture
def site(tmp_path):
    templates, static = tmp_path / "templates", tmp_path / "static"
    (templates / "partials").mkdir(parents=True)
    (templates / "base.html").write_text(_BASE)
    (templates / "partials" / "row.html").write_text("<tr><td>no assets</td></tr>")
    for asset in ("css/tailwind.css", "js/htmx.min.js", "js/row_menu.js"):
        (static / asset).parent.mkdir(parents=True, exist_ok=True)
        (static / asset).write_text("/* built */")
    return templates, static, StaticAssetCheck(templates, static)


def _touch_later(path: Path, text: str) -> None:
    """Rewrite a template with a strictly newer mtime, as an editor saving it would."""
    mtime = path.stat().st_mtime_ns
    path.write_text(text)
    os.utime(path, ns=(mtime + 1_000_000_000, mtime + 1_000_000_000))


@pytest.fixture
def ready(site, monkeypatch):
    """GET /health/ready on one running app instance, its checker pointed at the fake site."""
    from app.main import app

    monkeypatch.setattr("app.main._static_assets", site[2])
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    redis = AsyncMock()
    with (
        patch("app.main.AsyncSessionLocal", return_value=ctx),
        patch("app.main._get_redis", return_value=redis),
    ):
        client = TestClient(app)
        yield lambda: client.get("/health/ready")


def test_scan_finds_every_script_and_stylesheet_in_the_real_templates():
    check = StaticAssetCheck(Path("app/presentation/templates"), Path("app/static"))
    references = check.references()
    base = Path("app/presentation/templates/base.html").read_text()
    login = Path("app/presentation/templates/login.html").read_text()
    for template in (base, login):
        for tag in ('<script src="', 'rel="stylesheet" href="'):
            for chunk in template.split(tag)[1:]:
                assert chunk.split('"')[0].split("?")[0] in references
    assert "/static/js/row_menu.js" in references
    assert "/static/css/tailwind.css" in references


def test_scan_ignores_dynamic_and_non_script_references(site):
    assert site[2].references() == {
        "/static/css/tailwind.css",
        "/static/js/htmx.min.js",
        "/static/js/row_menu.js",
    }


def test_complete_directory_is_ready(ready):
    resp = ready()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "static_assets": "ok"}


def test_missing_asset_is_503_naming_it(site, ready):
    (site[1] / "js" / "row_menu.js").unlink()
    resp = ready()
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["missing_static_assets"] == ["/static/js/row_menu.js"]
    assert "/static/js/row_menu.js" in body["detail"]


def test_template_change_is_detected_while_the_app_keeps_running(site, ready):
    templates = site[0]
    base = templates / "base.html"
    assert ready().status_code == 200
    # The bind-mounted template gains a reference the static volume never received.
    _touch_later(
        base,
        _BASE.replace("</head>", '<script src="/static/js/new.js"></script></head>'),
    )
    resp = ready()
    assert resp.status_code == 503
    assert resp.json()["missing_static_assets"] == ["/static/js/new.js"]
    _touch_later(base, _BASE)
    assert ready().status_code == 200


def test_asset_removed_while_running_flips_readiness(site, ready):
    assert ready().status_code == 200
    (site[1] / "css" / "tailwind.css").unlink()
    assert ready().json()["missing_static_assets"] == ["/static/css/tailwind.css"]


def test_unchanged_templates_are_not_reparsed(site):
    check = site[2]
    check.missing()
    parsed = check.parse_count
    assert parsed == 2
    check.missing()
    check.missing()
    assert check.parse_count == parsed
    _touch_later(site[0] / "partials" / "row.html", "<tr></tr>")
    check.missing()
    assert check.parse_count == parsed + 1


def test_changed_content_under_an_existing_name_is_not_detected(site):
    """The documented limit: the check is existence-only."""
    (site[1] / "js" / "row_menu.js").write_text("/* stale build */")
    assert site[2].missing() == []


def test_missing_set_change_is_logged_once(site, caplog):
    check = site[2]
    (site[1] / "js" / "row_menu.js").unlink()
    with caplog.at_level("ERROR", logger="app.infrastructure.static_assets"):
        check.check()
        check.check()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "/static/js/row_menu.js" in errors[0].getMessage()


def test_reference_outside_the_static_dir_counts_as_missing(tmp_path):
    templates, static = tmp_path / "t", tmp_path / "s"
    templates.mkdir()
    static.mkdir()
    (tmp_path / "secret.js").write_text("x")
    (templates / "x.html").write_text('<script src="/static/../secret.js"></script>')
    assert StaticAssetCheck(templates, static).missing() == ["/static/../secret.js"]
