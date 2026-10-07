# health-endpoint Specification

## Purpose
Defines what the unauthenticated `GET /health` liveness probe reports, so orchestrators and operator tooling can check liveness and basic runtime mode without credentials.

## Requirements

### Requirement: Liveness probe reports whether stub provisioning is active

`GET /health` SHALL respond `200` without authentication, with a JSON body that contains `"status": "ok"` and a boolean `stub_terraform`. `stub_terraform` SHALL be `true` exactly when the process is configured to use the stub Terraform adapter, and `false` when it drives real infrastructure. Adding this field SHALL NOT remove or change any existing field, including the optional `slot`.

#### Scenario: Stub-mode stack
- **WHEN** `GET /health` is requested from a process running with the stub Terraform adapter
- **THEN** the response is `200` and its body contains `"status": "ok"` and `"stub_terraform": true`

#### Scenario: Real-provisioning stack
- **WHEN** `GET /health` is requested from a process configured for real Terraform/vCloud Director provisioning
- **THEN** the response is `200` and its body contains `"stub_terraform": false`

#### Scenario: Slot field is unaffected
- **WHEN** `GET /health` is requested from a process that has a deployment slot configured
- **THEN** the body still contains that `slot` value alongside `stub_terraform`

#### Scenario: No credentials required
- **WHEN** `GET /health` is requested with no session cookie and no API key
- **THEN** the response is `200` and does not redirect to the login page
