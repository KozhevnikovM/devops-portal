# Upgrade FastAPI and Starlette for OSC compatibility

## Why

The current dependency range in `requirements.txt` allows FastAPI versions below 0.116, and the resolved transitive dependency is `starlette==0.46.2`, whose wheel is blocked by OSC. Starlette is supplied by FastAPI, so an isolated Starlette pin could escape FastAPI's supported dependency range and leave application behavior unverified. The existing FastAPI range was retained because custom OpenAPI filtering depends on route representation that changed in newer FastAPI releases.

## What changes

- Select and document a FastAPI release whose declared Starlette constraints include a Starlette release accepted by OSC and exclude `0.46.2`.
- Update the direct FastAPI requirement and remove or revise the compatibility comment that only justified the old upper bound.
- Re-resolve the Python dependency set and verify the selected FastAPI/Starlette pair from package metadata.
- Add or update regression tests for startup, router inclusion, generated OpenAPI, custom schema filtering/visibility, and representative request/response behavior.
- Fix only compatibility issues caused by this framework upgrade.

## Capabilities

### New capabilities

- `backend-framework-compatibility`: Keep the backend's FastAPI and Starlette versions within a supported, OSC-accepted combination and protect framework-sensitive API behavior with tests.

### Modified capabilities

- None identified. The change preserves existing API behavior and adds compatibility validation.

## Impact

- `requirements.txt` and the resolved Python dependency artifacts, where maintained.
- Backend startup, routing, OpenAPI schema generation/filtering, and tests.
- Docker/backend clean build validation.

## Non-goals

- Unrelated dependency upgrades.
- Force-pinning Starlette outside FastAPI's declared dependency range.
- Refactoring OpenAPI filtering beyond changes needed to preserve current behavior.
