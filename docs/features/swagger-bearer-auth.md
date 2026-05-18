# Feature: Bearer Token Security Scheme in Swagger UI

## Goal

Add an **Authorize** button to the Swagger UI at `/docs` so developers can authenticate
with an API key and test protected endpoints without writing curl commands.

## What Changes

### `app/main.py` only

Override the FastAPI app's `openapi()` method to inject a `BearerAuth` HTTP security
scheme into the generated OpenAPI schema and apply it globally to all operations.

```python
from fastapi.openapi.utils import get_openapi

def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=..., version=..., routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "dp_<api_key>",
        }
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return app.openapi_schema

app.openapi = _custom_openapi
```

No changes to routes, dependencies, or auth logic.

## Expected Behaviour / Edge Cases

- A lock icon appears on every endpoint in `/docs`; clicking any lock or the top-level
  **Authorize** button opens a dialog where the user pastes their `dp_...` API key.
- Once authorized, all requests from Swagger UI include `Authorization: Bearer dp_...`.
- The existing session cookie auth path is unaffected — browser login still works normally.
- The public endpoints (`GET /auth/login`, `POST /auth/login`) are technically marked as
  requiring Bearer auth in the schema, but the actual dependency returns `None` rather than
  raising for unauthenticated access on those routes — no behaviour change.
- `persistAuthorization: true` added to `swagger_ui_parameters` so the key survives
  page refresh.
