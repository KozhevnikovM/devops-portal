## ADDED Requirements

### Requirement: Readiness requires the page assets to be served

`GET /health/ready` SHALL report a `static_assets` check. That check SHALL fail when any same-origin static asset (script or stylesheet) referenced by the page templates is absent from the static directory the application serves. While it fails, readiness SHALL return HTTP 503 and the response SHALL name the missing assets. The application SHALL also log the missing assets at error level when it starts. The existing dependency checks SHALL be unchanged.

#### Scenario: All referenced assets are present
- **WHEN** every static asset referenced by the page templates exists in the served static directory and the other readiness checks pass
- **THEN** `GET /health/ready` returns HTTP 200 and reports `static_assets` as ok

#### Scenario: Stale static volume after a template change
- **WHEN** the page templates reference a script that the served static directory does not contain, for example after the templates were updated without rebuilding the image that fills the static volume
- **THEN** `GET /health/ready` returns HTTP 503, its body names the missing script, and the startup log contains an error naming it

#### Scenario: Blue-green cutover is blocked by missing assets
- **WHEN** a blue-green deploy starts the inactive slot and that slot's served static directory lacks a referenced asset
- **THEN** the slot never reports ready, so traffic is not switched to it
