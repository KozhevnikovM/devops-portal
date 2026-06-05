"""Regression test for #186 — the OpenAPI schema hides HTML/HTMX routes.

The portal serves both a JSON API and many HTML/HTMX pages and fragments (all declared with
response_class=HTMLResponse). The schema at /openapi.json should expose only the JSON API
surface, so a central filter in app/main.py sets include_in_schema=False on every HTMLResponse
route.
"""
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app

# Real JSON API endpoints that must stay documented.
KEPT_PATHS = {
    "/api/images",
    "/api/hardware",
    "/api/users",
    "/api/bookings",
    "/api/bookings/{booking_id}/audit",
}

# HTML pages / HTMX fragments that must not appear in the schema (incl. the root HTMX booking
# routes, now HTML-only — the JSON API lives under /api/bookings).
HIDDEN_PATHS = {
    "/book/vm",
    "/book/namespace",
    "/bookings",
    "/bookings/{booking_id}/row",
    "/admin/catalog",
}


def test_openapi_excludes_html_routes_keeps_api():
    schema_paths = set(TestClient(app).get("/openapi.json").json()["paths"])

    assert KEPT_PATHS <= schema_paths, KEPT_PATHS - schema_paths
    assert HIDDEN_PATHS.isdisjoint(schema_paths), HIDDEN_PATHS & schema_paths


def test_every_html_route_is_marked_out_of_schema():
    # The invariant the filter enforces: no HTMLResponse route is left in the schema.
    leaked = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.response_class is HTMLResponse
        and route.include_in_schema
    ]
    assert leaked == [], leaked
