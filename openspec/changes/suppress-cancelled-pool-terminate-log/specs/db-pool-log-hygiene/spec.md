## Purpose

Keeps the application's structured logs free of benign, cancellation-driven SQLAlchemy connection-termination records — the noise emitted when a client disconnects mid-request — without hiding genuine database connection-pool errors.

## ADDED Requirements

### Requirement: Benign cancellation-driven pool termination records are suppressed

When a request holding a database connection is cancelled (e.g. the client disconnects, an SSE stream's tab closes, or a reverse proxy times out), SQLAlchemy's async connection pool cannot complete a graceful reset/terminate and emits a connection-termination log record carrying an `asyncio.CancelledError`. This is a benign artifact — the connection is force-closed correctly and no request is impaired. The system SHALL NOT surface such records in application logs.

Suppression SHALL apply uniformly across every process that installs the shared logging configuration (the `app`, `worker`, and `beat` processes).

#### Scenario: Pool termination record caused by CancelledError is dropped

- **WHEN** a connection-termination record is emitted by a SQLAlchemy connection-pool logger and its attached exception is an `asyncio.CancelledError`
- **THEN** the record is not written to the log output

#### Scenario: Suppression applies in worker and beat processes too

- **WHEN** the same cancellation-driven pool-termination record is emitted in the `worker` or `beat` process (which share the application's logging configuration)
- **THEN** the record is not written to the log output there either

### Requirement: Genuine pool errors remain visible

Suppression SHALL be narrowly scoped to cancellation artifacts only. Any connection-pool log record whose exception is not an `asyncio.CancelledError`, and any record that carries no exception, SHALL be emitted unchanged at its original level.

#### Scenario: Pool error with a non-cancellation exception is preserved

- **WHEN** a SQLAlchemy connection-pool logger emits a record whose attached exception is anything other than an `asyncio.CancelledError` (e.g. an operational/connection error)
- **THEN** the record is written to the log output unchanged, at its original level

#### Scenario: Pool records without an exception are preserved

- **WHEN** a SQLAlchemy connection-pool logger emits a record that carries no exception information
- **THEN** the record is written to the log output unchanged

#### Scenario: Non-pool logs are unaffected

- **WHEN** a record carrying an `asyncio.CancelledError` is emitted by any logger that is not a SQLAlchemy connection-pool logger
- **THEN** the record is written to the log output unchanged

### Requirement: Pool exhaustion remains independently observable

Suppressing the benign termination record SHALL NOT reduce visibility of connection-pool exhaustion. Pool exhaustion manifests through a distinct signal — a failed in-flight request (a pool-timeout error surfaced to the caller) — which this suppression does not touch.

#### Scenario: Pool-timeout failure on a live request is still reported

- **WHEN** a request fails because it could not obtain a connection from the pool within the pool timeout
- **THEN** that failure is still logged and surfaced to the caller, unaffected by the termination-record suppression
