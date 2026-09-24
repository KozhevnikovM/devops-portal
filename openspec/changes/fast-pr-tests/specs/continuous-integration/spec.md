## Purpose

Defines the repository's automated pull-request gates and the local commands that reproduce them.

## ADDED Requirements

### Requirement: Every pull request runs the fast Python test suite

Every pull request targeting `main` SHALL produce a CI check named `fast-tests` that executes the repository's non-integration Python tests. The check SHALL fail when pytest reports a test failure, collection error, or execution error.

#### Scenario: Pull request passes the fast suite
- **WHEN** a pull request targets `main` and all tests selected by `pytest tests/ -m "not integration"` pass
- **THEN** the `fast-tests` check succeeds

#### Scenario: Pull request introduces a failing unit test
- **WHEN** a pull request targets `main` and any selected test fails
- **THEN** the `fast-tests` check fails and pytest output identifies the failing test

#### Scenario: Pull request is updated
- **WHEN** a new commit is pushed while an older `fast-tests` run for the same pull request is still running
- **THEN** the obsolete run is cancelled and the newest commit is tested

#### Scenario: Manual runs use a ref-based concurrency identity
- **WHEN** `fast-tests` is started manually for two different refs
- **THEN** each run uses its own ref-based concurrency group and neither run cancels the other

### Requirement: The fast test gate is reproducible locally

The repository SHALL document one canonical command, `pytest tests/ -m "not integration"`, and CI SHALL execute that same command after installing `requirements-dev.txt`.

#### Scenario: Developer reproduces CI locally
- **WHEN** a developer installs `requirements-dev.txt` in a clean supported Python environment and runs the documented command
- **THEN** pytest selects the same test tier and uses the same repository configuration as the `fast-tests` CI job

### Requirement: The fast test gate has no external service dependency

The `fast-tests` job SHALL run without PostgreSQL, Redis, Celery workers, or other service containers. It SHALL NOT configure `TEST_POSTGRES_URL` or silently skip a selected test because a service is unavailable.

#### Scenario: CI runs in an isolated Python environment
- **WHEN** `fast-tests` runs with no service containers or service connection variables
- **THEN** the selected non-integration suite completes without attempting to connect to PostgreSQL or Redis

#### Scenario: A service-backed test is added without the integration marker
- **WHEN** a selected test unexpectedly requires PostgreSQL or Redis
- **THEN** `fast-tests` fails, exposing the classification or isolation error instead of provisioning that service implicitly

### Requirement: The workflow uses least privilege

The `fast-tests` workflow SHALL receive only read access to repository contents and SHALL NOT require write permissions, repository secrets, deployment environments, or privileged service containers.

#### Scenario: Pull request from an untrusted branch
- **WHEN** `fast-tests` runs for pull-request code
- **THEN** the workflow can check out and test the code without access to write-capable credentials or repository secrets
