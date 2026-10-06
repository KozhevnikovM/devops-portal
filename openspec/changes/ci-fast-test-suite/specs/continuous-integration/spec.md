## MODIFIED Requirements

### Requirement: Every pull request runs the fast Python test suite

Every pull request targeting `main` SHALL produce a CI check named `fast-tests` that executes the repository's non-integration Python tests after performing a lightweight application import smoke check. The check SHALL fail when the application import smoke check fails, or when pytest reports a test failure, collection error, or execution error.

#### Scenario: Pull request passes the fast suite
- **WHEN** a pull request targets `main` and all tests selected by `pytest tests/ -m "not integration"` pass
- **THEN** the `fast-tests` check succeeds

#### Scenario: Pull request introduces a failing unit test
- **WHEN** a pull request targets `main` and any selected test fails
- **THEN** the `fast-tests` check fails and pytest output identifies the failing test

#### Scenario: Pull request introduces an application import error
- **WHEN** a pull request introduces an import error, broken dependency, or invalid module initialization
- **THEN** the lightweight application import smoke check fails and the `fast-tests` check terminates with an actionable error

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while an older `fast-tests` run for the same pull request is still running
- **THEN** the obsolete run is cancelled and the newest commit is tested

#### Scenario: Manual runs use a ref-based concurrency identity
- **WHEN** `fast-tests` is started manually for two different refs
- **THEN** each run uses its own ref-based concurrency group and neither run cancels the other

### Requirement: The fast test gate has no external service dependency

The `fast-tests` job SHALL run without PostgreSQL, Redis, Celery workers, or other service containers. It SHALL NOT configure `TEST_POSTGRES_URL` or silently skip a selected test because a service is unavailable.

#### Scenario: CI runs in an isolated Python environment
- **WHEN** `fast-tests` runs with no service containers or service connection variables
- **THEN** the selected non-integration suite completes without attempting to connect to PostgreSQL or Redis

#### Scenario: A service-backed test is added without the integration marker
- **WHEN** a selected test unexpectedly requires PostgreSQL or Redis
- **THEN** `fast-tests` fails, exposing the classification or isolation error instead of provisioning that service implicitly

#### Scenario: Application import smoke check executes without services
- **WHEN** the application import smoke check runs in an environment without PostgreSQL or Redis
- **THEN** the application imports cleanly without attempting to connect to external services
