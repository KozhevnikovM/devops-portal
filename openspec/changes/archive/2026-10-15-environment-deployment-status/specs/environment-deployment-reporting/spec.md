## Purpose

Expose the current application deployment reported by Jenkins independently of Environment resource lifecycle, using an authorized reporting API and a safe, read-only view in the existing Environments table.

## ADDED Requirements

### Requirement: An environment has either no deployment report or one complete report

The system SHALL store the last accepted report as `deployment_ref`, `deployment_commit`, `deployment_status`, `deployment_frontend_url`, `deployment_admin_url` and `deployment_updated_at`. All six SHALL be null on existing and newly created environments until the first report. A report SHALL contain all six non-null values; its status SHALL be exactly `DEPLOYING`, `SUCCESS` or `FAILED`, independent of infrastructure lifecycle. The server SHALL generate an aware UTC acceptance timestamp. Existing authenticated environment list, get, namespace lookup and creation JSON responses SHALL expose all six fields, retaining existing read permissions and fields. No free-form deployment description or history SHALL be introduced.

#### Scenario: Independent states
- **WHEN** Jenkins reports FAILED for a READY environment
- **THEN** its deployment status is FAILED and infrastructure status remains READY

#### Scenario: No initial report
- **WHEN** an existing environment is migrated or a new environment is created
- **THEN** all six deployment fields are null in its JSON response and no deployment success is inferred

### Requirement: Jenkins replaces a validated report atomically

The system SHALL accept complete reports at `PATCH /api/v1/environments/{environment_id}/deployment-info` and the legacy `/api/environments/{environment_id}/deployment-info` alias. The body SHALL require all five writable fields as non-null strings. The server SHALL reject unknown fields, including caller-supplied timestamps and unrelated environment fields. A successful update SHALL return 200 with the existing environment JSON shape including the accepted report. Omitted, null, invalid or oversized values SHALL return 422 without changing any report field or its timestamp.

Ref SHALL contain 1–512 characters, with at least one non-whitespace character and no control characters. Commit SHALL be a full hexadecimal Git SHA-1 (40 characters) or SHA-256 (64 characters); abbreviated hashes SHALL be rejected. Both URLs SHALL be at most 2048 characters, absolute HTTP/HTTPS URLs with a host, without embedded username/password, whitespace or control characters. Values SHALL NOT be silently trimmed, truncated or rewritten; accepted URL strings SHALL be preserved exactly. Ref is display text, not a Git expression to resolve. The server SHALL NOT fetch supplied URLs or Git metadata.

Each accepted update SHALL replace all five values and the server timestamp in one transaction. Concurrent reports SHALL never combine values from different reports. Jenkins SHALL serialize authoritative reporting per Environment and prevent overtaken builds from publishing later transitions; the portal SHALL display the last accepted report without inferring build ordering.

#### Scenario: Complete transition
- **WHEN** an authorized Jenkins identity reports a full ref, full commit, DEPLOYING and both URLs, then a complete SUCCESS report
- **THEN** each report is persisted together with its server-generated timestamp and the final response contains the complete SUCCESS report

#### Scenario: Invalid report preserves the previous report
- **WHEN** a report omits any required field, supplies null, an abbreviated commit, unsupported status, over-limit text, unsafe URL scheme, credentials in a URL or a caller timestamp
- **THEN** the API responds 422 and all previously stored report fields and timestamp remain unchanged

#### Scenario: Exact service URLs
- **WHEN** a valid URL includes a path, query or fragment
- **THEN** the returned and linked URL preserves that exact string without normalization or invented paths

#### Scenario: Concurrent reports
- **WHEN** two complete reports overlap
- **THEN** the saved report is one complete accepted report with its matching timestamp, never a mixture of the two

### Requirement: Reporting uses existing identity authorization and preserves environment state

The reporting endpoint SHALL authenticate through the existing API-key/user authentication mechanism and authorize the existing management scope: owner, dispatcher that created the environment on the owner's behalf, or admin. Merely knowing an Environment UUID or reading its namespace lookup SHALL NOT grant write permission. No blanket admin requirement SHALL apply. Missing/invalid authentication SHALL follow the existing JSON API authentication behavior; an authenticated caller outside management scope SHALL receive 403, and a nonexistent environment SHALL return 404. Authorization SHALL precede revealing lifecycle-specific 409 errors.

Authorized reports to an Environment whose derived lifecycle is RELEASED SHALL return 409. For other lifecycle states, deployment reporting SHALL remain independent of infrastructure state. Eligibility SHALL be checked against current persisted lifecycle at the write, including concurrent completion of release. The report SHALL survive subsequent release. A report update SHALL change no name, ownership, ordering key, resource, child status, TTL or expiry and SHALL NOT initiate deployment or provisioning.

Jenkins documentation SHALL resolve UUID with `GET /api/environments/by-namespace/{namespace_name}?cluster={cluster_name}` using both namespace and cluster. Existing lookup behavior for other consumers SHALL remain unchanged.

#### Scenario: Non-admin CI identity
- **WHEN** Jenkins uses the API key of the owner or creating dispatcher
- **THEN** it can update the report without admin rights

#### Scenario: Lookup does not authorize a write
- **WHEN** another authenticated identity locates the environment through namespace lookup and submits a report
- **THEN** the write returns 403 and leaves the report unchanged

#### Scenario: Released and missing environments
- **WHEN** an authorized caller reports to a RELEASED environment or a nonexistent UUID
- **THEN** the API returns respectively 409 or 404 without writing deployment metadata

#### Scenario: Release completion races the write
- **WHEN** the last child becomes RELEASED before the report's authoritative lifecycle check
- **THEN** the report returns 409; a report serialized before release completion commits first and remains as the last report in the final released state

#### Scenario: Deployment report does not alter the stack
- **WHEN** an authorized report changes ref, commit, deployment status and URLs on a non-RELEASED environment
- **THEN** its name, lifecycle, child resources, TTL, expiry and list position remain unchanged

### Requirement: Reporting preserves pooled-resource queue progress

A deployment report SHALL NOT cause a QUEUED booking to be skipped during pooled-resource promotion. Reporting SHALL preserve FIFO selection and SHALL NOT leave a free matching resource with an unpromoted eligible booking because its write overlaps resource release. These guarantees SHALL hold while the report transaction is open, not only after a retry or another resource release.

#### Scenario: Oldest queued child during reporting
- **WHEN** reporting for an environment is paused in its write transaction, its child is the oldest of multiple eligible QUEUED bookings and a matching pooled resource is released
- **THEN** promotion selects that oldest child instead of a newer booking, exactly once, and reporting does not prevent its eventual assignment

#### Scenario: Sole queued child during reporting
- **WHEN** reporting for an environment is paused in its write transaction, its child is the only eligible QUEUED booking and a matching pooled resource is released
- **THEN** the child is promoted exactly once and no free-resource/queued-booking pair is stranded after the overlapping operations, without another release or promotion retry

### Requirement: Deployment is a compact read-only column in the existing table

The Environments table SHALL add Deployment immediately after Resources and before Owner, preserving Name, lifecycle Status, resource badges, Expires, Actions and existing page composition. Each report cell SHALL display ref first, its commit abbreviated to seven characters on a separate line, deployment status and local update time on the next line, then compact `Frontend ↗` and `Admin ↗` links. Ref and commit SHALL use existing monospace styling; long refs SHALL truncate with ellipsis without hiding the independent hash. Preview text SHALL use existing compact gray styling and cell padding, distinct from lifecycle status badges.

Links SHALL open exactly the accepted Jenkins URLs in a new tab with `rel="noopener noreferrer"` and accessible labels identifying the environment. Link activation SHALL NOT toggle the disclosure. Before the first report the cell SHALL show `No deployment information`, without active service links or a fabricated report. Browser controls SHALL NOT offer deployment editing or new deployment actions.

#### Scenario: Long branch and PR references
- **WHEN** the report ref is a long feature branch or PR-1234
- **THEN** the first line displays that ref with truncation where necessary, the seven-character hash remains on its own line and status/time and two service links remain visible

#### Scenario: Safe read-only rendering
- **WHEN** a ref contains HTML-like text and the user opens the cell
- **THEN** it is rendered as escaped text, no script executes and no deployment editor or Save control appears

#### Scenario: Service link activation
- **WHEN** a user activates Frontend or Admin
- **THEN** the corresponding exact supplied URL opens with the required tab protection and the report disclosure does not toggle

### Requirement: Full report disclosure is accessible and survives dynamic table behavior safely

A keyboard-accessible disclosure SHALL show a read-only overlay over the table without expanding its row. It SHALL show full ref, full commit, deployment status, both complete URLs and the full update date/time in the user's timezone consistent with existing timezone formatting. A RELEASED environment SHALL retain its report and label this view `Last report (released environment)`.

The panel SHALL use existing menu backgrounds, borders, rounding and shadow, stay within the viewport and avoid table overflow clipping. It SHALL safely wrap long values, support keyboard activation, Escape and outside-click close, and return focus to its trigger when that trigger remains. Row replacement/removal or section replacement SHALL close and remove any associated panel; focus SHALL return to the replacement trigger when available, otherwise a stable list control. No detached panel, stale content or accumulating listeners SHALL remain.

#### Scenario: Keyboard and narrow viewport
- **WHEN** a keyboard user opens a report near the table edge on a narrow viewport and presses Escape
- **THEN** the full wrapped panel stays visible without clipping and closing returns focus to the trigger

#### Scenario: Row refresh while open
- **WHEN** SSE or reconciliation replaces a row with an open report
- **THEN** the old panel closes and is removed, focus is restored safely and reopening shows the updated report

#### Scenario: Released report
- **WHEN** a released environment with a saved report is shown and its disclosure opened
- **THEN** the complete retained report is labelled Last report (released environment)

### Requirement: Reports refresh through bounded existing list infrastructure

Every rendered deployment value, including both URLs and timestamp, SHALL participate in row change detection and list-safe projections. Successful committed reports SHALL issue the existing environment row notification; failed updates SHALL publish no change. Notification transport failure SHALL NOT undo a committed report. Database-backed reconciliation SHALL recover missed notifications under existing convergence rules and permissions, including READY and FAILED environments and rows appended by Load more.

Reporting SHALL preserve existing pagination, creation ordering, filters, projection safety, query counts, child limits and reconciliation batch bounds. It SHALL add no per-row polling, full-list reload or Git/Jenkins lookup. Deployment status SHALL NOT change lifecycle-based reconciliation scheduling. Released rows SHALL retain their final report and existing terminal subscription behavior.

#### Scenario: Missed SSE notification
- **WHEN** a READY environment receives a report but its event is lost
- **THEN** reconciliation updates its displayed report within the existing convergence bound without reloading the list or changing pagination

#### Scenario: URL-only report change
- **WHEN** a complete accepted report changes only a service URL and timestamp
- **THEN** row change detection refreshes both the compact link and complete disclosure values

#### Scenario: Bounded appended pages
- **WHEN** a report changes on an appended page and its row is reconciled
- **THEN** it refreshes in place with unchanged filters/cursors, within existing statement and child-read bounds and without exposing booking secrets

### Requirement: Production rollout is additive and documented

The system SHALL add nullable deployment storage through a migration suitable for a populated production database, without backfilling synthetic reports or timestamps or rewriting all rows. The portal SHALL deploy independently of Jenkins reporting and preserve existing API fields/consumers and lifecycle behavior. API/OpenAPI, admin and user documentation SHALL describe validation, permissions, empty/released states and a Jenkins example publishing complete DEPLOYING then SUCCESS/FAILED reports with both URLs. Documentation SHALL state that keys inherit identity privileges, metadata must contain no credentials/tokens and stale-build prevention belongs to Jenkins.

#### Scenario: Portal deployed before Jenkins
- **WHEN** a production database is migrated and the portal upgraded while Jenkins reporting is disabled
- **THEN** existing environments remain usable with no deployment information and unchanged leases, resources and existing API fields
