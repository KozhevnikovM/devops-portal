## Context

See proposal.md for motivation and issue #535 for the approved UI. The service already has an Environment dataclass/ORM mapping, API serializers mounted on `/api/v1` and legacy `/api`, API keys that resolve to users, `can_manage` authorization, bounded environment list projections and section reconciliation. `environment_row_version` hashes explicit environment and child field lists. RELEASED rows stop subscribing/reconciling. Deployment information must therefore be included in every projection and version, and cannot change after release.

The table currently has six columns inside `overflow-x-auto`; an absolutely positioned child panel alone would be clipped. Resource statuses derive lifecycle, rather than a persisted environment status. Existing release paths update children and do not all take the parent lock.

## Goals / Non-Goals

**Goals:** integrate a complete application report into the existing clean architecture, keep the report consistent under overlapping writes and lifecycle updates, preserve bounded reads and make disclosure work with dynamic row replacement.

**Non-Goals:** new API-key scoping infrastructure, new roles, new deployment workflow, history or build sequence tokens; changes to existing read permissions; implementing Jenkins jobs or plugins. Existing management authorization scopes an identity's ability to report; it is not a new restriction on every capability of that identity's API keys.

## Decisions

### 1. Typed report with nullable additive storage

Add a domain deployment-status enum distinct from BookingStatus and a report value/validation boundary without framework imports. Environment exposes the six agreed field names; use a complete report value for mutation to prevent partial updates. ORM columns are nullable with no defaults: bounded ref (512), commit (64), status string (validated enum), URLs (2048 each), timezone-aware timestamp. Do not add a native PostgreSQL enum, new table or report index. Nullable columns avoid backfill/table rewrite and support old application versions during rollout.

The common `_to_entity` mapper and every explicit environment projection carry all six fields. New fields have null domain defaults so creation and existing constructors remain compatible. Where practical add a database check enforcing all-null or complete, and status validity; add it NOT VALID then validate separately with a production-aware lock timeout. The API/domain validation remains authoritative for detailed URL/hash rules. Never perform an unbounded validation scan inside a long exclusive-lock migration.

Alternative free-form text cannot support stable status rendering or independently validated links. JSON storage is unnecessary for six stable fields and makes explicit projections less clear.

### 2. Complete replacement endpoint and existing authorization

Add `UpdateEnvironmentDeploymentUseCase`, an EnvironmentRepositoryPort update operation and composition-root wiring. FastAPI validates a dedicated request with all five strict string fields required, extra fields forbidden and the bounds/formats in the spec. Run equivalent business validation at the domain boundary. Validate URLs without replacing their original strings: HTTP(S), hostname, no credentials, no whitespace/controls, 2048 characters maximum. This preserves Jenkins paths, queries and fragments exactly; no URL fetch occurs.

Use existing `require_user` and `can_manage`, including ordinary owner and creating-dispatcher keys. Dedicated CI identities must own the target or have created it on behalf of its owner; an arbitrary service user does not gain permission by lookup. Existing admin behavior is retained but is not needed. Keys still inherit the identity's other existing privileges. A deployment-only token with arbitrary per-environment grants would require a separate security feature and is not claimed here. Browser sessions retain the existing authenticated API behavior, but no browser writer is added.

Mount the endpoint on both router prefixes and document the versioned path in OpenAPI. Use the current serializer for 200 responses and add nullable report fields to list/get/lookup/create responses. Authorization runs before lifecycle conflict checks. The lookup example always includes cluster; changing optional cluster for existing consumers is unnecessary.

### 3. Transactional report replacement and release completion

Use the Environment row as the shared serialization point. A report transaction takes only that parent row lock, refreshes existence/management authorization, then reads current child statuses without row-locking them. Under PostgreSQL READ COMMITTED, perform that read as a fresh statement after acquiring the parent lock, not from an earlier ORM collection or snapshot. If derived RELEASED, roll back and return 409; otherwise generate the timestamp and update only the six parent report columns in one commit. Do not flush an old full Environment snapshot, restart TTL, touch child values or hold the lock during network/provisioning operations. Reporting must acquire no child row locks, including on QUEUED bookings; shortening a conflicting child lock would not fix SKIP LOCKED promotion.

A parent lock is sufficient only when release completion participates in the same protocol. Extend the async `BookingRepository.update_status` and sync `sync_update_status` paths: before any environment child's transition to RELEASED, resolve its immutable environment ID using a non-locking read, acquire its Environment row lock, then refresh/revalidate the child and perform its status/audit changes. Hold the parent lock through the commit that makes the child RELEASED. Standalone bookings keep their existing behavior. Audit every runtime path that can make derived lifecycle become RELEASED (manual/force release, queued cancellation, TTL, teardown, restart recovery, direct status writes and any allowed child removal), route it through this guarded boundary or apply the same protocol. Never acquire the parent after holding a child lock or flushing a child mutation; callers with pending writes must establish the parent-first order before those writes. This is a required implementation change, not an assumption about current release code. Reuse the parent-first order of existing lease operations; keep transactions short and release the lock at commit before event publication, resource promotion or external work.

If a final-child release owns the parent first, reporting waits for its commit, reads fresh statuses and rejects with 409. If reporting owns it first, it commits its complete report before final-child release can commit, so the final RELEASED rendering contains that report. Reporting during RELEASING remains allowed until derived RELEASED; no release-start guard or new persisted lifecycle flag is introduced. Queue promotion keeps its existing candidate selection and FIFO semantics: while a report holds the parent, the oldest or sole QUEUED child's booking row remains unlocked and selectable. If a promotion path subsequently needs the parent, it must end its candidate-locking transaction before taking that lock, preserving the existing lock order. Do not introduce a child-to-parent dependency. Use deterministic PostgreSQL barriers/events, rather than sleeps, to prove both queue cases and both release/report orderings.

Concurrent reports serialize on the parent lock; each accepted operation returns its own complete persisted report. Capture the response snapshot while holding the parent lock, then return that snapshot only after commit succeeds; do not commit and re-fetch a mutable report that another request may already have replaced. Jenkins must prevent late obsolete builds from publishing. Neither commit hashes nor receipt timestamps can establish Jenkins build precedence, so server-side stale-build detection would be misleading without expanding the contract.

### 4. Post-commit delivery and bounded projection parity

Publish an environment-only row-change event with owner/creator routing after commit, using the existing event envelope and `_rows_to_refresh` support for environment IDs. Do not emit a fake child event or classify it as progress-only (which intentionally skips environment refreshes). Publication failure must not turn a successful commit into a failed API response.

Include all six report fields in `_ENVIRONMENT_FIELDS` and in initial, appended, single-row, SSE and reconciliation projection paths. Existing lifecycle-based live classes and rotating batches remain unchanged: DEPLOYING on READY is still a settled infrastructure row. Fallback convergence uses the existing multi-page bound, not an unconditional one-minute promise. Preserve the environment reconciliation five-statement maximum, child-read limits and no-secret projections. Add only parent columns, never per-row reads or external requests.

### 5. Read-only partial and detached overlay lifecycle

Keep environments.html composition and add a dedicated deployment partial in environment_row.html, with its header after Resources in environment_list_section.html. Use `px-4 py-3`, monospace ref/hash, `text-xs text-gray-300`, `text-[10px] text-gray-500` for time and existing neutral gray colors; deployment status does not use lifecycle `status-*` badges. Ref truncates; hash remains independent. Both compact links are siblings of the disclosure trigger with environment-specific accessible labels, `_blank` and `noopener noreferrer`.

Use a real button (`aria-expanded`, `aria-controls`) and one shared disclosure controller. Render escaped report content using existing templates, then attach the open panel to a body-level overlay host outside table overflow; do not interpolate Jenkins data into executable JavaScript or unescaped HTML. Apply `bg-gray-900 border border-gray-700 rounded shadow-lg`. Position relative to the trigger, clamp to viewport, wrap values, constrain height with scrolling, and reposition or close on scroll/resize. Format full date/time through `as_tz(current_user.timezone)`; compact time uses the same timezone.

Use delegated activation for appended/replaced rows, Escape/outside close and predictable focus return. Before HTMX/SSE row swaps, row removals and section replacement, dispose the old panel and reset state. When its trigger was replaced, focus the corresponding new trigger after the swap; if absent use a stable list/filter control. Unchanged reconciliation rows preserve an open panel. Test detached panels and listener counts over repeated swaps.

Update environment-specific six-column spans to seven, including Load more, new-row indicators and reconciliation error placeholders; shared fragments receive the correct environments column count without changing booking tables.

## Risks / Trade-offs

- Existing keys inherit broader user privileges → document a dedicated least-privileged identity and its ownership/dispatcher scope; do not advertise deployment-only keys.
- A release writer bypasses the shared parent lock → audit async/sync RELEASED writes and applicable child deletion paths; test manual/force/TTL/queued cancellation/teardown/recovery paths and both overlap orders on PostgreSQL.
- QUEUED child locks conflict with FIFO promotion → reporting takes no child row locks; pause it under the parent lock and deterministically prove oldest and sole queued children remain selectable/promoted without a retry or later resource release.
- Old processes do not implement the release protocol → keep reporting disabled until old app/worker processes and in-flight release work are drained or replaced; nullable columns alone do not make mixed-version reporting safe.
- Outdated Jenkins builds can overwrite newer reports → Jenkins example serializes the whole deployment/report sequence per environment and suppresses obsolete failure/finally callbacks; portal does not arbitrate builds.
- Native URL validators may normalize strings → validate separately and retain original accepted strings; test exact URLs, unsafe schemes, HTML-like refs and credential-bearing URLs.
- Overlay outlives HTMX row → central controller cleanup and browser tests for replacement, removal and filter change.
- Added column increases table width → retain current horizontal scrolling and compact typography, verify narrow viewports rather than redesigning the page.

## Migration Plan

1. Add a migration after current Alembic head with six nullable columns, no defaults/backfill or new indexes. Verify migration-chain integrity, generated PostgreSQL DDL and upgrade on populated data. Keep validation scans separate from exclusive-lock DDL; use short lock timeout/retry during rollout.
2. Deploy schema before the new app. Old app versions can omit columns; existing and newly ordered rows remain null until reporting starts. Smoke-test pages and existing API consumers before enabling Jenkins.
3. Before enabling reporting, replace/drain all old app and Celery worker processes and in-flight release transactions so every release-completion writer uses the shared parent-lock protocol. Keep Jenkins reporting disabled throughout mixed-version rollout. Then enable reporting for a dedicated authorized CI identity. Resolve namespace and cluster to UUID, send all five fields on every DEPLOYING/SUCCESS/FAILED call, and use Jenkins credential binding without logging keys or metadata containing secrets. Configure per-environment serialization across the deployment and reporting sequence.
4. On application rollback stop Jenkins reports first, deploy the previous portal version and leave additive columns/data intact. A destructive schema downgrade is optional only after all new readers/writers are stopped and report-data loss is accepted; do not make column removal a routine rollback requirement.
