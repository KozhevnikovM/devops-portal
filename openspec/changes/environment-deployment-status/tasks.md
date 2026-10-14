## 1. Domain and production storage

- [ ] 1.1 Add the independent deployment-status enum and complete report validation/value model; verify unit tests for SHA-1/SHA-256, ref bounds/control characters, strict statuses and exact safe URLs, including invalid/partial reports.
- [ ] 1.2 Extend Environment and its ORM/common mapper with six nullable fields and add the additive migration; verify migration-chain tests, PostgreSQL upgrade on populated data, existing/new null values and no synthetic timestamps or row-rewriting defaults.
- [ ] 1.3 Carry all six fields through explicit list-safe and by-ID projections; verify projection parity and existing environment pagination/query/child bounds without credentials or logs.

## 2. Authorized atomic reporting API

- [ ] 2.1 Add the repository port/method and update use case with existing management authorization, parent-first/current-child locking, authoritative RELEASED check and report-only atomic commit; verify unit tests for owner/creating-dispatcher/admin acceptance, unrelated user denial, 404/409 and unchanged name, ownership, TTL, expiry and child state.
- [ ] 2.2 Add a strict complete-report request and PATCH endpoint on both versioned and legacy mounts; verify 200 serializer parity, required non-null strings, extra-field/timestamp rejection, 422 rollback, missing/invalid authentication and no session fallback for an invalid bearer token under existing authentication rules.
- [ ] 2.3 Add all six nullable fields to existing environment list/get/by-namespace/create JSON serialization and OpenAPI report schema; verify existing response fields/read permissions and namespace-plus-cluster resolution remain compatible on both mounts.
- [ ] 2.4 Add PostgreSQL transaction tests for overlapping full reports, failed-write rollback, report versus final-child release (manual and TTL paths), concurrent lease extension and queued promotion; verify each response reflects its own accepted snapshot, complete stored reports, no acceptance after observed RELEASED, no deadlock/stranded queue item and unchanged lease/resource behavior.

## 3. Notifications and reconciliation

- [ ] 3.1 Publish an environment-only row-change notification with owner/creator routing after successful commit; verify the SSE environment refresh path and that rollback emits nothing while notification failure leaves a committed report successful.
- [ ] 3.2 Include all report fields in environment_row_version; verify each field independently affects the digest, and initial/Load more/action/SSE/reconciliation renders agree for the same state.
- [ ] 3.3 Exercise missed-event recovery on READY/FAILED and appended-page rows, including URL-only changes; verify existing batch scheduling, five-statement environment bound, bounded children, permissions, list ordering and cursors remain intact.

## 4. Approved read-only table and disclosure

- [ ] 4.1 Add the dedicated deployment partial/header between Resources and Owner with ref, separate short hash, independent status/local time, safe Frontend/Admin links and no-report state; verify template tests for approved order/styling, exact link targets, protected new tabs, accessible labels, escaping and no deployment editor.
- [ ] 4.2 Update environment-specific column counts, including Load more, new-row indicator and reload-required placeholders; verify seven-column rendering without changing booking/namespace tables or environments.html composition.
- [ ] 4.3 Implement the shared viewport-clamped overlay controller and full report template, with timezone formatting, long-value wrapping and released label; verify browser keyboard activation, Escape, outside close, focus return, narrow viewport, horizontal scroll and independent link activation.
- [ ] 4.4 Integrate disposal/focus restoration with row/section replacement and removal; verify browser behavior over SSE, reconciliation, filter changes and appended rows, no orphan panel/listener accumulation, unchanged-row panel preservation and updated content on reopening.

## 5. Documentation and verification

- [ ] 5.1 Update docs/api-reference.md, docs/admin-guide.md and relevant user documentation with validation bounds/hash formats, identity-based permission limits, empty/released states, rollout/rollback and namespace+cluster discovery; verify examples match both supported mounts and OpenAPI.
- [ ] 5.2 Provide a Jenkins example with bound credentials, complete DEPLOYING then SUCCESS/FAILED payloads, exact service URLs and per-environment serialization/obsolete-callback suppression; verify every shown report includes all five fields and no secret is logged or stored in metadata. Actual Jenkins job/plugin implementation remains outside this change.
- [ ] 5.3 Run the non-integration suite, focused PostgreSQL concurrency/migration/reconciliation tests, browser disclosure scenarios, Tailwind rebuild and required py-review/CI checks; record results and any environment limitations in the implementation PR.
- [ ] 5.4 After implementation approval, sync delta specs and archive the completed change on the code PR branch; verify strict OpenSpec validation and archived tasks before merging the implementation PR.
