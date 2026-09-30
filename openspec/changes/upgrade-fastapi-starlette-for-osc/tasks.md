## 1. Dependency selection and update

- [ ] 1.1 FastAPI 0.116.2 metadata accepts Starlette `>=0.40,<0.49`; a clean resolution selected 0.48.0, not `0.46.2`. Confirm that OSC accepts the selected artifact before closing this task; its internal approval status cannot be queried from this environment.
- [x] 1.2 Update the FastAPI constraint and replace the obsolete compatibility comment with the supported Starlette range.
- [x] 1.3 No Python lock or generated dependency artifact is maintained in this repository. A fresh virtual environment install of `requirements.txt` and `requirements-dev.txt` succeeded without resolver conflicts.

## 2. Compatibility regression coverage

- [x] 2.1 Added lifespan startup and health endpoint coverage; verified included API router registration in the framework regression test.
- [x] 2.2 Extended OpenAPI regression coverage for API visibility, hidden HTML routes, and the custom bearer security scheme.
- [x] 2.3 Existing API route and health endpoint tests verify representative request/response handling and TestClient behavior.

## 3. Verification

- [ ] 3.1 All 1,160 non-integration backend tests pass, including the added compatibility coverage. Integration tests require PostgreSQL on `localhost:5433`, which is not available here.
- [ ] 3.2 Dependency resolution confirms Starlette 0.48.0, but Docker is not installed in this environment, so the clean image build remains unverified.
