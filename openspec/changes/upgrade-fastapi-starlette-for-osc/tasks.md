## 1. Dependency selection and update

- [ ] 1.1 Determine an OSC-accepted Starlette release and select a FastAPI release whose declared Starlette constraints include it; verify package metadata and avoid `starlette==0.46.2`.
- [ ] 1.2 Update the FastAPI constraint in `requirements.txt`; remove or revise the old compatibility comment based on the verified route representation.
- [ ] 1.3 Re-resolve or regenerate the repository's Python dependency artifacts, if maintained, and confirm a clean install has no resolver conflicts.

## 2. Compatibility regression coverage

- [ ] 2.1 Add or update tests for application startup and included API routers.
- [ ] 2.2 Add or update tests for generated OpenAPI and custom filtering/visibility rules.
- [ ] 2.3 Validate representative request/response handling and test client behavior; fix only upgrade-caused incompatibilities.

## 3. Verification

- [ ] 3.1 Run existing backend tests and the new compatibility tests.
- [ ] 3.2 Build the backend/Docker image from a clean dependency environment and confirm the resolved Starlette version is not `0.46.2`.
