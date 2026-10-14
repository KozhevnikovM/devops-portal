## Why

Production users cannot see which application revision Jenkins deployed to an Environment or whether its deployment succeeded. Issue #535 defines a structured Jenkins report and an approved read-only column in the current Environments table, separate from infrastructure lifecycle.

## What Changes

- Persist nullable `deployment_ref`, `deployment_commit`, `deployment_status`, `deployment_frontend_url`, `deployment_admin_url` and server-generated `deployment_updated_at`; no report exists until Jenkins sends one.
- Add `PATCH /api/v1/environments/{environment_id}/deployment-info` and its existing legacy `/api/environments/...` alias. Every report supplies all five input fields and replaces the previous report atomically. Reject invalid/partial reports with 422, forbidden writes with 403, missing environments with 404 and RELEASED environments with 409.
- Reuse API-key authentication and existing owner/creating-dispatcher/admin management authorization. Jenkins uses a dedicated identity authorized for its environments; namespace-plus-cluster lookup resolves the UUID. No new role or key-scope administration system.
- Add all six fields to existing authenticated environment JSON responses, with null values before the first report. Keep lifecycle, TTL, names, resources and existing read permissions unchanged.
- Add the approved compact Deployment column between Resources and Owner: ref, separate seven-character commit, deployment status/time and Frontend/Admin links; a keyboard-accessible overlay shows the complete read-only report.
- Carry reports through bounded projections, row versions, post-commit notifications and reconciliation without changing pagination or query/child bounds.
- Provide a production-safe additive migration, API/OpenAPI and user/admin documentation, and Jenkins reporting examples including stale-build responsibility.

## Capabilities

### New Capabilities

- `environment-deployment-reporting`: complete validated Jenkins reports, authorized atomic replacement, lifecycle independence, production rollout and read-only presentation with live refresh.

### Modified Capabilities

- `environment-listing`: explicitly allow additive deployment fields in the existing JSON list contract while retaining existing fields, released visibility and derived status.

## Impact

Domain Environment/report types; application update use case and repository port; SQLAlchemy mapping, additive Alembic migration and environment projections; API router/composition root; environment templates and a disclosure controller; row-version hashing and event publication. Tests cover API/permissions, atomic persistence and release races on PostgreSQL, safe links, browser keyboard/disclosure behavior and bounded reconciliation. No new runtime dependency is required. Jenkinsfile/plugin implementation, manual notes/editing, Git lookup, deployment history and server-side build arbitration are out of scope.
