# Feature: Offline-reachable static assets

## Goal

Remove all CDN dependencies from the UI so the portal works in environments
with no outbound internet access. Three assets are currently loaded from
external CDNs at runtime:

| Asset | Current source |
| :--- | :--- |
| Tailwind CSS | `https://cdn.tailwindcss.com` (Play CDN — development-only, not for production) |
| HTMX | `https://unpkg.com/htmx.org@1.9.12` |
| HTMX SSE extension | `https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js` |

## What Changes

### New files committed to the repo

```
app/static/
  css/
    tailwind.css          # generated from templates via Tailwind standalone CLI
  js/
    htmx.min.js           # downloaded from unpkg (pinned to 1.9.12)
    htmx-sse.js           # downloaded from unpkg (pinned to 1.9.12)
```

**Tailwind CSS** is generated once (with internet access) using the Tailwind
standalone CLI scanning our templates for used utility classes. The output is
committed — no re-generation needed on build or deploy.

**HTMX files** are downloaded once and committed at the pinned version already
used by the templates.

### Code changes

- `app/main.py` — mount `StaticFiles` at `/static` pointing to `app/static/`
- `app/presentation/templates/base.html` — replace three CDN `<script>`/`<link>`
  tags with `/static/...` paths
- `docker-compose.yml` — no change needed (static dir is already volume-mounted
  with `. :/app`)

### No other changes

- No DB migrations.
- No API changes.
- No new dependencies in `requirements.txt`.

## Edge Cases

- Tailwind CSS must be regenerated whenever new utility classes are added to
  templates. The generation command will be documented in `CLAUDE.md`.
- The committed CSS uses Tailwind's JIT output (only classes found in templates),
  keeping the file small (~15 KB vs 3.5 MB full build).
