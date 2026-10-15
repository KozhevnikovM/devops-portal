## ADDED Requirements

### Requirement: Readiness requires the page assets to be served

`GET /health/ready` SHALL report a `static_assets` check, evaluated against the current templates and static directory on each request, so that template or asset changes made while the application keeps running are reflected without a restart. That check SHALL fail when any same-origin static asset (script or stylesheet) referenced by the page templates is absent from the static directory the application serves. While it fails, readiness SHALL return HTTP 503 and the response SHALL name the missing assets. The application SHALL also log the missing assets at error level when it starts and whenever the set of missing assets changes. The check verifies that each referenced file exists; it is not required to detect a changed file whose name is unchanged. The existing dependency checks SHALL be unchanged.

#### Scenario: All referenced assets are present
- **WHEN** every static asset referenced by the page templates exists in the served static directory and the other readiness checks pass
- **THEN** `GET /health/ready` returns HTTP 200 and reports `static_assets` as ok

#### Scenario: Stale static volume after a template change
- **WHEN** the page templates reference a script that the served static directory does not contain, for example after the templates were updated without rebuilding the image that fills the static volume
- **THEN** `GET /health/ready` returns HTTP 503, its body names the missing script, and the log contains an error naming it

#### Scenario: Template changed while the application keeps running
- **WHEN** readiness has returned 200, and then, without restarting the application, a page template is changed to reference a script the served static directory does not contain
- **THEN** the next `GET /health/ready` returns HTTP 503 naming that script
- **AND** after the template is reverted, the next call returns HTTP 200 again

#### Scenario: Asset removed while the application keeps running
- **WHEN** readiness has returned 200, and then a referenced script is removed from the served static directory without restarting the application
- **THEN** the next `GET /health/ready` returns HTTP 503 naming that script

#### Scenario: Changed content under an existing name is not covered
- **WHEN** a referenced script's content changes without a rebuild but a file with that name already exists in the served static directory
- **THEN** the `static_assets` check still reports ok; this skew is outside the check's scope

#### Scenario: Blue-green cutover is blocked by missing assets
- **WHEN** a blue-green deploy starts the inactive slot and that slot's served static directory lacks a referenced asset
- **THEN** the slot never reports ready, so traffic is not switched to it
