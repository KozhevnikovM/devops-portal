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
    "/api/v1/images",
    "/api/v1/hardware",
    "/api/v1/static-vms",
    "/api/v1/roles",
    "/api/v1/environment-blueprints",
    "/api/v1/environments",
    "/api/users",
    "/api/v1/bookings",
    "/api/v1/bookings/{booking_id}/audit",
}

# HTML pages / HTMX fragments that must not appear in the schema (incl. the root HTMX booking
# routes, now HTML-only — the JSON API lives under /api/bookings).
HIDDEN_PATHS = {
    "/book/vm",
    "/book/namespace",
    "/bookings",
    "/bookings/{booking_id}/row",
    "/bookings/{booking_id}/credentials",  # #478: credential-bearing fragment
    "/bookings/{booking_id}/audit",
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


def test_framework_route_representation_preserves_router_and_openapi_behavior():
    """FastAPI upgrades must keep included API routes inspectable by the HTML filter."""
    from fastapi.routing import APIRoute

    api_route_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/")
    }
    assert "/api/v1/bookings" in api_route_paths

    schema = app.openapi()
    assert "/api/v1/bookings" in schema["paths"]
    assert "/bookings" not in schema["paths"]
    assert schema["security"] == [{"BearerAuth": []}]
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"


def test_application_lifespan_starts_and_health_endpoint_responds(monkeypatch):
    from app import main

    # Startup's database recovery is covered separately; isolate the ASGI lifespan contract.
    monkeypatch.setattr(main, "_seed_admin_user", lambda: None)
    monkeypatch.setattr(main, "_recover_in_progress_bookings", lambda: None)
    monkeypatch.setattr(main, "_recover_stuck_releases", lambda: None)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
