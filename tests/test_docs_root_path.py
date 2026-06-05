"""Regression test for #183 — Swagger UI honours ROOT_PATH behind a reverse-proxy subpath.

Before the fix, `app = FastAPI(...)` had no root_path, so the docs page at `/docs` always
referenced the bare `/openapi.json`. Behind a `/dp` subpath proxy the browser then fetched
`https://host/openapi.json` (the proxy root) and Swagger UI failed with "Not Found
/openapi.json". After the fix the app is built with `root_path=settings.ROOT_PATH`, so
setting `ROOT_PATH=/dp` makes the docs page request `/dp/openapi.json`.
"""
import importlib

from fastapi.testclient import TestClient

import app.main
from app.config import settings


def _docs_html_with_root_path(root_path: str) -> str:
    """Rebuild the app with the given ROOT_PATH and return its served /docs HTML."""
    settings.ROOT_PATH = root_path
    importlib.reload(app.main)
    return TestClient(app.main.app).get("/docs").text


def test_swagger_ui_openapi_url_honours_root_path():
    original = settings.ROOT_PATH
    try:
        subpath_html = _docs_html_with_root_path("/dp")
        root_html = _docs_html_with_root_path("")
    finally:
        # Restore the shared app singleton so later tests see the default root path.
        settings.ROOT_PATH = original
        importlib.reload(app.main)

    # Subpath deploy: the docs page fetches the schema under the prefix.
    assert "/dp/openapi.json" in subpath_html

    # Default/direct deploy: bare schema URL, never the prefixed one.
    assert "/openapi.json" in root_html
    assert "/dp/openapi.json" not in root_html
