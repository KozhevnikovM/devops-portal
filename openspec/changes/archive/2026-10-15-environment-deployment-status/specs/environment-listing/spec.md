## MODIFIED Requirements

### Requirement: The JSON environments list is unchanged

The JSON environments list (`GET /api/v1/environments` and the legacy unversioned `GET /api/environments`) SHALL keep its existing fields, read permissions, ordering and released-environment behavior. It SHALL still return released environments with the same derived `status`. It SHALL additionally expose `deployment_ref`, `deployment_commit`, `deployment_status`, `deployment_frontend_url`, `deployment_admin_url` and `deployment_updated_at` under the environment-deployment-reporting contract; these are null before the first accepted report. Deployment status SHALL NOT replace the derived lifecycle `status`.

#### Scenario: JSON list still includes released environments
- **WHEN** a user calls `GET /api/v1/environments` and one of their environments is fully released
- **THEN** the response includes that environment with `status` `RELEASED`

#### Scenario: Additive report fields
- **WHEN** a caller lists environments through either JSON route
- **THEN** existing fields and derived statuses remain present and all six deployment fields accompany each environment, null for an unreported environment
