## Purpose

Defines the operator-run load-test tooling under `loadtest/`: how it refuses unsafe targets, how it seeds test data, what traffic it simulates, and what counts as a passing run. It doubles as a regression guard for the SSE/DB-pool exhaustion class of bug (#407).

## ADDED Requirements

### Requirement: Load tooling refuses non-loopback targets by default

The seed script and the load generator SHALL each check the target before sending any request that changes state. The target passes the host check only when its host is `localhost`, an IPv4 address in `127.0.0.0/8`, or `::1`. The check SHALL NOT resolve hostnames through DNS. Any other host SHALL be refused unless the operator sets the remote-target override to that exact host (case-insensitive, port ignored). When the target is refused, the tool SHALL exit non-zero, or stop the load run before any simulated user starts, with a message that names the target and explains how to override.

#### Scenario: Loopback target accepted
- **WHEN** the target is `http://localhost:8000` or `http://127.0.0.1:8000` and the target reports stub mode
- **THEN** the tool proceeds

#### Scenario: Remote target refused by default
- **WHEN** the target is `https://portal.example.com` and no override is set
- **THEN** the tool exits non-zero before any login, seeding or simulated user request, and the message names `portal.example.com`

#### Scenario: Hostname that resolves to loopback is still refused
- **WHEN** the target host is a non-literal name other than `localhost`, such as `host.docker.internal`, and no override is set
- **THEN** the tool refuses the target without consulting DNS

#### Scenario: Override must name the target
- **WHEN** the remote-target override is set to a host other than the target's host
- **THEN** the tool still refuses the target

#### Scenario: Matching override accepted
- **WHEN** the remote-target override equals the target's host and the target reports stub mode
- **THEN** the tool proceeds

### Requirement: Load tooling refuses targets not in stub provisioning mode

Before sending any request that changes state, the seed script and the load generator SHALL request `GET /health` from the target. They SHALL proceed only when the response is `200` and reports `stub_terraform: true`. A non-`200` response, an unreachable target, a missing field, or a `false` value SHALL each be treated as refusal. No override SHALL relax this check.

#### Scenario: Real-provisioning target refused
- **WHEN** the target passes the host check but `GET /health` reports `stub_terraform: false`
- **THEN** the tool exits non-zero before any login, seeding or simulated user request

#### Scenario: Older server without the field refused
- **WHEN** `GET /health` returns `200` without a `stub_terraform` field
- **THEN** the tool refuses the target

#### Scenario: Remote override does not bypass stub check
- **WHEN** the remote-target override matches the target host but the target reports `stub_terraform: false`
- **THEN** the tool refuses the target

### Requirement: Seeding is idempotent and reports failures

The seed script SHALL create or reuse a fixed set of test accounts, `loadtest-0001` through `loadtest-1000`, all with the same known test password and the `user` role. It SHALL give each account a quota high enough that quota limits do not cap the test. It SHALL create or reuse one VM image and one hardware config, plus a pool of static VMs and a pool of namespaces. Before creating any account or pool entry it SHALL check whether that item already exists, so re-running the script against a seeded stack creates nothing new and still succeeds. Any create that the server rejects, including a `200` response that carries the HTMX error-retarget headers, SHALL be reported. If any such failure occurred, the script SHALL exit non-zero.

#### Scenario: First run on an empty stack
- **WHEN** the seed script runs against a fresh stub stack
- **THEN** it exits `0`, and the 1000 accounts, the catalog entries and both pools exist

#### Scenario: Re-run on a seeded stack
- **WHEN** the seed script runs a second time against the same stack
- **THEN** it creates no new accounts, catalog entries or pool entries, and exits `0`

#### Scenario: Server rejects a pool entry
- **WHEN** a static-VM or namespace create returns `200` with HTMX error-retarget headers
- **THEN** the script reports that entry as failed and exits non-zero at the end

### Requirement: Simulated traffic models open tabs and active ordering

The load generator SHALL simulate two kinds of user, each logged in as a distinct seeded account:

- **Passive watchers**, about 85% of users: each holds one `GET /events/stream` connection open for its whole lifetime. Each also periodically loads the bookings dashboard or the environments page, using both the "mine" and the "all" list, and repeats that page's background reconcile poll.
- **Active orderers**, about 15% of users: each also holds an SSE connection open, and repeatedly runs one booking flow. The flow orders a booking, mostly `VM` and sometimes `STATIC_VM` or `NAMESPACE`, then polls it until it is no longer in a transient state, holds it, and releases it. Each also repeats the reconcile poll of its own bookings list.

A simulated reconcile poll SHALL send the same parameters a browser sends for the page it last loaded:
- `r=<id>.<version>` for a bounded batch of the displayed live rows, chosen with the browser's batch-selection rule. That rule puts in-flight rows first and reserves settled-row slots, uses the page's advertised maximum and settled minimum, and rotates through rows across successive polls;
- `newest=<list key>` of the first displayed row, when any row is displayed.

After each response, the simulated client SHALL take the row versions returned in it as the versions it holds, as the browser does.

Every JSON API request SHALL ask for a JSON response, so that an unauthenticated request is recorded as a failure rather than as a successful redirect to the login page. Each request type SHALL appear under its own name in the run's statistics. The SSE connection SHALL be recorded as a failure when it cannot be established. An ordered booking that ends up `QUEUED` because the pool is empty SHALL NOT be recorded as a failure.

#### Scenario: Session survives plain-http localhost
- **WHEN** a simulated user logs in to `http://localhost:8000` and the server marks the session cookie `Secure`
- **THEN** that user's later requests, including the SSE connection, are still authenticated

#### Scenario: Lost authentication is a failure
- **WHEN** a simulated user's session is no longer valid and it makes a JSON API request
- **THEN** the request is recorded as a failure, not as a success

#### Scenario: Booking lifecycle completes
- **WHEN** an active orderer orders a VM booking against the stub stack
- **THEN** the booking reaches `READY`, is released with a `202` response, and each step is recorded under its own request name

#### Scenario: Reconcile poll names displayed rows
- **WHEN** a simulated user's last loaded list page displays live rows and its reconcile poll fires
- **THEN** the request carries a non-empty batch of `r=<id>.<version>` values no larger than the page's advertised maximum, plus `newest` set to the first displayed row's list key

#### Scenario: Reconcile batch rotates
- **WHEN** a page displays more live rows than the advertised maximum and the poll fires repeatedly without a page reload
- **THEN** successive requests name different rows, following the browser's rotation rule, so every displayed row is named over time

#### Scenario: Empty list
- **WHEN** the last loaded list page displays no rows
- **THEN** the reconcile request carries neither `r` nor `newest`, as the browser's request would

#### Scenario: Pool empty
- **WHEN** an active orderer orders a pooled resource while the pool is exhausted
- **THEN** the booking is accepted as `QUEUED`, no failure is recorded, and the user does not try to release it

### Requirement: Load runs have documented pass criteria

The pool-exhaustion signature is the log line prefix `sqlalchemy.exc.TimeoutError: QueuePool limit`. Only that exact signature counts; other timeouts do not.

The tooling documentation SHALL define two kinds of run and their criteria.

A **smoke run** (100 users) passes only when both hold:
- its statistics show a 0% failure rate across all request names;
- the app and worker logs from the run's time window contain no pool-exhaustion signature.

A **characterization run** (1000 users) passes only when all of these hold:
- it runs for its full configured duration;
- the app and worker logs from the run's time window contain no pool-exhaustion signature;
- its failure rate and per-request-name latencies are recorded;
- every request name with a non-zero failure count is attributed, in the recorded results, to a cause other than DB-pool exhaustion, and is linked to a follow-up issue.

A pool-exhaustion signature in either kind of run SHALL fail that run.

The documentation SHALL give the command that checks the logs for exactly the run's window, matching only the pool-exhaustion signature. It SHALL say to do the smoke run before the characterization run. It SHALL state the host prerequisites the load generator needs, including the open-file-descriptor limit.

#### Scenario: Smoke run with any failure
- **WHEN** a 100-user smoke run records any failed request
- **THEN** the run fails, even if the logs contain no pool-exhaustion signature

#### Scenario: Characterization run with attributed failures
- **WHEN** a 1000-user run completes its full duration with some failed requests, no pool-exhaustion signature, and each failing request name attributed to a non-pool cause with a linked follow-up issue
- **THEN** the run passes and its failure rate is recorded

#### Scenario: Pool exhaustion at any scale
- **WHEN** the logs from a run's window contain `sqlalchemy.exc.TimeoutError: QueuePool limit`
- **THEN** the run fails, whatever its scale or failure rate

#### Scenario: Unrelated timeout is not counted
- **WHEN** the logs from a run's window contain a timeout error that lacks the pool-exhaustion signature
- **THEN** the log check does not report it as pool exhaustion

#### Scenario: Operator verifies a run
- **WHEN** an operator follows `loadtest/README.md` after a run
- **THEN** they can decide pass or fail from the CSV statistics, the scoped log check and the recorded results, without other knowledge
