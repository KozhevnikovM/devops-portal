# Backend framework compatibility

## ADDED Requirements

### Requirement: OSC-compatible FastAPI and Starlette dependency pair

The backend MUST resolve FastAPI and Starlette to versions that satisfy FastAPI's declared dependency constraints, are accepted by OSC, and do not resolve to the blocked `starlette==0.46.2` artifact. The project MUST NOT rely on a Starlette pin outside the selected FastAPI release's supported range.

#### Scenario: Clean dependency resolution

- **WHEN** dependencies are installed from a clean environment using the repository's supported install procedure
- **THEN** dependency resolution completes without conflicts
- **AND** the resolved FastAPI and Starlette versions satisfy their published dependency metadata
- **AND** the resolved Starlette version is not `0.46.2` and is accepted by OSC

### Requirement: Preserve API routing and OpenAPI behavior

The backend MUST preserve existing router inclusion, endpoint request/response behavior, OpenAPI generation, and custom schema filtering/visibility when the framework pair changes.

#### Scenario: Application starts with the upgraded framework pair

- **WHEN** the backend starts with the resolved FastAPI and Starlette versions
- **THEN** startup completes successfully
- **AND** all existing API routers are registered

#### Scenario: OpenAPI schema is generated and filtered

- **WHEN** the OpenAPI schema is generated
- **THEN** the schema remains valid
- **AND** the project's existing custom filtering and visibility rules produce the expected result

#### Scenario: Existing endpoints handle requests

- **WHEN** representative existing API requests are handled by the upgraded application
- **THEN** their established request and response behavior remains unchanged
