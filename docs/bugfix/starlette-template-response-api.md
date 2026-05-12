# Bugfix: 500 on all routes — unhashable type 'dict' in Jinja2 cache

## Root Cause

Starlette 1.0.0 (pulled in by `fastapi>=0.111.0`) changed the `TemplateResponse`
signature. The old API passed the template name first and the context dict second:

```python
# Old API (Starlette < 1.0.0)
templates.TemplateResponse("index.html", {"request": request, "bookings": bookings})
```

In Starlette 1.0.0, `request` is the first positional argument and the context
dict no longer includes `request`:

```python
# New API (Starlette >= 1.0.0)
templates.TemplateResponse(request, "index.html", {"bookings": bookings})
```

With the old call style, Starlette 1.0.0 receives the context dict as the `name`
argument (expected to be a `str`). Jinja2 then tries to use that dict as a cache
key, which raises `TypeError: unhashable type: 'dict'`.

## What Changes

**`app/presentation/routes/bookings.py`** — three `TemplateResponse` / `render`
call sites updated to the Starlette 1.0.0 API:

1. `GET /` index route
2. `POST /bookings` fragment response
3. SSE `render()` call — `request` removed from context (template doesn't use it)

## Expected Behaviour After Fix

`GET /` returns 200 with the booking list page. `POST /bookings` returns a 201
HTML fragment. SSE stream delivers rendered row updates correctly.

## No other changes

- No DB migrations required.
- No API contract changes (same endpoints, same HTML/JSON outputs).
