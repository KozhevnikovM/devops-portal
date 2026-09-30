## 1. Dependency selection and update

- [x] 1.1 FastAPI 0.116.2 resolves to Starlette 0.48.0, within its published `>=0.40,<0.49` range and excluding `0.46.2`; the review confirmed OSC accepted the resolved artifact without errors.
- [x] 1.2 Updated the FastAPI constraint and replaced the obsolete route-representation comment.
- [x] 1.3 No Python lock or generated dependency artifact is maintained. A clean `requirements-dev.txt` install resolves without conflicts.

## 2. Compatibility regression coverage

- [x] 2.1 Added application lifespan startup and health endpoint coverage; verified included API router registration.
- [x] 2.2 Extended OpenAPI coverage for router visibility, hidden HTML routes, and the custom bearer security scheme.
- [x] 2.3 Representative API route and health requests pass with the upgraded framework pair.

## 3. Verification

- [x] 3.1 The fast backend suite (1,160 tests) and PostgreSQL integration suite both pass in PR CI.
- [x] 3.2 The external clean Docker image build completed successfully (confirmed in PR review on 2026-09-30).
