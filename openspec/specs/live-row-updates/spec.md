# live-row-updates Specification

## Purpose
Defines how row-visible booking changes are announced to live UI subscribers. Lifecycle changes are delivered immediately. High-frequency progress output is coalesced per booking, so notification volume stays bounded while the final visible state is still guaranteed to be delivered.

## Requirements

### Requirement: Lifecycle changes are announced immediately

Every committed change to a booking that is not a progress-output line SHALL publish a row-changed notification right after the commit, without throttling or delay. This covers status transitions (including READY, FAILED, RELEASED), clearing or setting the status message outside progress output, label and TTL changes, and queue promotion.

#### Scenario: Terminal status during a progress burst
- **WHEN** a booking is emitting progress lines faster than the coalescing window and then transitions to READY
- **THEN** a row-changed notification for the READY transition is published immediately, and is not delayed until the coalescing window ends

#### Scenario: Status message cleared at a step boundary
- **WHEN** the status message is cleared after a Terraform apply or destroy completes
- **THEN** a row-changed notification is published immediately

### Requirement: Progress notifications are coalesced per booking

Row-changed notifications caused by progress output (Ansible, startup-script, SSH-wait, or Terraform progress lines) SHALL be rate-limited per booking. A progress producer is one provisioning or teardown task execution for a booking. A producer SHALL publish at most one progress notification for that booking in each coalescing window. A booking normally has a single active producer, so this is at most one per window per booking. When producers overlap for the same booking (for example a teardown that starts while post-provision configuration is still streaming output), each producer SHALL be bounded independently, so the per-booking rate is at most one per window per overlapping producer. The window is configurable and defaults to 750 ms. Coalescing notifications SHALL NOT cause any progress line to go unpersisted. Lines are persisted in batched commits, as defined by the `progress-persistence` capability, not one commit per line. When their persistence succeeds, every progress line's content reaches the provisioning log. The only lines that may be missing are the ones `progress-persistence` allows to be lost: a batch discarded by a failed barrier flush, and the unflushed tail after a hard worker kill, within the bounds that capability defines. A progress notification SHALL be submitted once per successfully committed progress batch, after that commit, and SHALL then be coalesced as described here. A batch that fails to commit SHALL NOT produce a notification.

#### Scenario: A burst of 100 lines within one second
- **WHEN** a single producer records 100 progress lines for a booking within one second, with the default 750 ms window
- **THEN** at most 3 progress row-changed notifications are published for that booking during and immediately after the burst
- **AND** all 100 lines are persisted to the booking's provisioning log, given that their progress flushes succeed

#### Scenario: Isolated progress line
- **WHEN** a booking records a progress line, that line is committed immediately as the leading line after a quiet period, and no progress notification has been published for it within the current window
- **THEN** a row-changed notification is published immediately for that line

#### Scenario: Progress line right after a lifecycle notification
- **WHEN** a lifecycle notification for a booking is published (for example the status message is cleared at a step boundary) and the next progress line for that booking follows shortly after, within one coalescing window
- **THEN** that progress line's notification is published as soon as its batch commits, because a lifecycle notification does not start or extend a progress coalescing window

#### Scenario: Progress line after a lifecycle notification while a progress publish is still in flight
- **WHEN** a progress notification for a booking is still being published (Redis slower than the window), a lifecycle notification for that booking is published, and then a new progress line for that booking is committed before the in-flight publish returns
- **THEN** the new line's progress notification is not published concurrently with the in-flight one
- **AND** it is published as soon as the in-flight publish returns, without waiting for a further coalescing window

#### Scenario: Overlapping producers for one booking
- **WHEN** two producers record bursts of progress lines for the same booking concurrently
- **THEN** each producer publishes at most one progress notification per window for that booking, plus its own trailing notification
- **AND** each producer's final progress state is still announced

#### Scenario: One notification per committed batch
- **WHEN** a producer commits one progress batch containing 50 lines
- **THEN** exactly one progress notification is submitted to the coalescer for that batch, not 50

#### Scenario: Coalescing disabled
- **WHEN** the coalescing window is configured as 0
- **THEN** every committed progress batch publishes a row-changed notification immediately
- **AND** with the progress flush interval also configured as 0, every progress line publishes a notification immediately, as before batching was introduced

### Requirement: The final progress state of a burst is always announced

When progress lines are suppressed inside a coalescing window, the system SHALL publish one trailing row-changed notification for that booking no later than one window after the last suppressed line. If a publish for that booking is still in flight at that point (Redis slower than the window), the trailing notification SHALL be published as soon as that publish returns. A producer SHALL NOT have more than one progress notification for the same booking in flight at a time. Subscribers can then render the final progress state without waiting for another progress line, a lifecycle change, or the fallback poll.

#### Scenario: Burst followed by silence
- **WHEN** a booking records a burst of progress lines and then records nothing for several seconds
- **THEN** a trailing row-changed notification for that booking is published within one coalescing window after the last line of the burst
- **AND** a subscriber rendering the booking at that point sees the last recorded progress message

#### Scenario: Lifecycle change supersedes a pending trailing notification
- **WHEN** a trailing progress notification is pending for a booking and a lifecycle change for that booking is published
- **THEN** the pending trailing progress notification is discarded on a best-effort basis, because the lifecycle notification already causes a render of the latest state
- **AND** at most one progress notification for lines recorded *before* the lifecycle change, one that was already being published when the lifecycle change happened, may be delivered after the lifecycle notification
- **AND** progress lines recorded *after* the lifecycle change are new progress. They are announced normally (see "Progress line right after a lifecycle notification" and its in-flight variant) and do not count toward that bound

#### Scenario: Redis slower than the coalescing window
- **WHEN** a progress notification for a booking takes longer than one coalescing window to publish, while more progress lines for that booking are recorded and then a lifecycle change is published
- **THEN** no second progress notification for that booking starts until the first has returned
- **AND** at most one progress notification for lines recorded before the lifecycle change (the one already in flight) is delivered after the lifecycle notification

#### Scenario: A late progress notification never shows stale state
- **WHEN** a progress notification for a booking is delivered after a lifecycle notification for the same booking
- **THEN** a subscriber rendering the booking in response shows the booking's current state, including the lifecycle change, because notifications only signal that a booking changed and carry no row state

### Requirement: Bookings are throttled independently

Coalescing state SHALL be tracked per booking. Progress output from one booking SHALL NOT delay, suppress, or merge notifications for any other booking.

#### Scenario: Two bookings bursting concurrently
- **WHEN** booking A and booking B each record a burst of progress lines within the same window
- **THEN** each booking receives its own leading notification and its own trailing notification
- **AND** each notification identifies only its own booking

### Requirement: Notifications identify their kind

Each row-changed notification SHALL carry a kind of either `progress` or `lifecycle`, so subscribers can treat progress-only changes differently. Subscribers SHALL treat a notification without a kind as `lifecycle`.

#### Scenario: Progress notification kind
- **WHEN** a notification is published because of progress output
- **THEN** its payload carries kind `progress` along with the booking id and environment id

#### Scenario: Legacy payload without a kind
- **WHEN** a subscriber receives a row-changed notification that has no kind field
- **THEN** it processes the notification as a lifecycle notification

### Requirement: Progress notifications refresh only the booking row

When a subscriber receives a row-changed notification of kind `progress`, it SHALL refresh only the booking row, even when the notification carries an environment id. Progress output changes only the booking's status message and provisioning log, and the environment row shows neither. When a subscriber receives a notification of kind `lifecycle`, it SHALL refresh the booking row and, if the notification carries an environment id, the parent environment row. A notification with no kind or with an unrecognised kind SHALL be handled as `lifecycle`, so the environment row is never left stale by a payload the subscriber does not understand. Authorization for each refreshed row is unchanged: a row is rendered onto a connection only if that connection's user may manage it.

#### Scenario: Progress line for an environment child booking
- **WHEN** a subscriber receives a `progress` notification for a booking that belongs to an environment
- **THEN** it pushes a refreshed booking row for that booking
- **AND** it does not render or push the environment row

#### Scenario: Progress line for a standalone booking
- **WHEN** a subscriber receives a `progress` notification for a booking that belongs to no environment
- **THEN** it pushes a refreshed booking row for that booking and nothing else

#### Scenario: Status transition for an environment child booking
- **WHEN** a subscriber receives a `lifecycle` notification for a booking that belongs to an environment (for example the child becomes READY or FAILED)
- **THEN** it pushes a refreshed booking row for that booking
- **AND** it pushes a refreshed environment row for the parent environment, so the environment's aggregate status and child list reflect the change

#### Scenario: Status transition for a standalone booking
- **WHEN** a subscriber receives a `lifecycle` notification for a booking that belongs to no environment
- **THEN** it pushes a refreshed booking row for that booking and no environment row

#### Scenario: Notification without a kind for an environment child booking
- **WHEN** a subscriber receives a notification that has no kind field, or an unrecognised kind, and carries both a booking id and an environment id
- **THEN** it pushes both the refreshed booking row and the refreshed environment row

### Requirement: Publishing remains best-effort

A failure to publish any row-changed notification, including a trailing progress notification, SHALL be logged and SHALL NOT fail or roll back the write that triggered it, and SHALL NOT interrupt the provisioning or teardown task.

#### Scenario: Redis unavailable during a trailing publish
- **WHEN** the trailing progress notification for a booking fails to publish because Redis is unreachable
- **THEN** the error is logged, the provisioning task continues unaffected, and later notifications for that booking are still attempted

### Requirement: Notifications carry per-row routing metadata without sensitive values

Every row-changed notification, whether `progress` or `lifecycle`, SHALL carry the owner user id of the booking it concerns. When the booking was created by a dispatcher or admin on the owner's behalf, it SHALL also carry that creator's user id. A `lifecycle` notification for a booking that belongs to an environment SHALL additionally carry the owner user id of that environment and, if it has one, the environment's creator user id. These are taken from the environment itself, not the child booking. A child booking's creator can differ from its environment's: a namespace booking adopted into an environment keeps its own creator, while the environment records whoever ordered it. Apart from the booking id, environment id, kind and these user ids, the payload SHALL NOT contain any other booking or user data. In particular it SHALL NOT contain usernames, passwords, IP addresses, labels, status messages, provisioning output or secret values.

#### Scenario: Lifecycle notification for a user's own standalone booking
- **WHEN** a standalone booking owned by user U, with no creator recorded, changes status
- **THEN** the published notification carries U's user id as the booking owner, no booking creator, and no environment routing

#### Scenario: Progress notification for a booking ordered by a dispatcher
- **WHEN** a dispatcher D ordered a booking on behalf of user U, and that booking records a progress line
- **THEN** the published progress notification carries U's user id as the booking owner and D's user id as the booking creator

#### Scenario: Lifecycle notification for an adopted environment child
- **WHEN** user U owns a standalone namespace booking with no creator, dispatcher D orders an environment on U's behalf that adopts that namespace, and the adopted booking then changes status
- **THEN** the published notification carries U as the booking owner and no booking creator
- **AND** it carries U as the environment owner and D as the environment creator

#### Scenario: Payload contents are limited
- **WHEN** any row-changed notification is published
- **THEN** its payload contains only the booking id, the environment id (or none), the kind, the booking owner and creator ids, and, for a lifecycle notification of an environment child, the environment owner and creator ids

### Requirement: Subscribers discard unrelated rows before any database lookup

A live-update subscriber SHALL decide, for each row a notification would refresh (the booking row, and for a lifecycle notification the environment row), whether its user could manage that row. It SHALL decide from that row's own routing metadata alone, applying the same rule as row rendering: the user is an admin, the row's owner, or the row's recorded creator. The booking row SHALL be judged only by the booking routing, and the environment row only by the environment routing. A row the user could not manage SHALL be skipped without opening a database session or looking it up. A row the user could manage SHALL be loaded and authorized from the database as before. The database-backed check stays authoritative, and the metadata check SHALL only ever skip work, never grant visibility. Rows pushed to an authorized user SHALL be identical to those pushed before this change.

#### Scenario: Unrelated user's notification
- **WHEN** a connection for ordinary user A receives a notification in which A appears as neither the owner nor the creator of the booking, nor (if environment routing is present) of the environment
- **THEN** the notification is discarded without any database session being opened or any booking or environment being looked up
- **AND** nothing is pushed to A's connection

#### Scenario: Owner still receives the row
- **WHEN** a connection for user U receives a notification whose booking owner is U
- **THEN** the booking is loaded from the database, authorized, and its rendered row is pushed exactly as before

#### Scenario: Creating dispatcher still receives the row
- **WHEN** a connection for dispatcher D receives a notification whose booking owner is user U and whose booking creator is D
- **THEN** the rendered booking row is pushed to D's connection

#### Scenario: Dispatcher does not receive other users' rows
- **WHEN** a connection for dispatcher D receives a notification in which D appears as neither the owner nor the creator of the booking or of the environment
- **THEN** the notification is discarded without any database lookup

#### Scenario: Admin receives every row
- **WHEN** a connection for an admin receives a notification for a booking the admin neither owns nor created
- **THEN** the booking (and, for a lifecycle notification of an environment child, the environment) is loaded and its rendered row is pushed, as before

#### Scenario: Dispatcher-created environment that adopted the owner's namespace
- **WHEN** dispatcher D ordered an environment for user U that adopted U's pre-existing standalone namespace booking (booking creator absent or another dispatcher, environment creator D), and a connection for D receives a lifecycle notification for that adopted booking
- **THEN** the booking row is skipped without a booking lookup, because D does not manage the booking itself
- **AND** the environment is loaded, authorized, and its rendered row is pushed to D's connection

#### Scenario: Unrelated lifecycle notification for an environment child
- **WHEN** a connection for ordinary user A receives a `lifecycle` notification for an environment child booking whose booking and environment routing both name user B as owner and not A as creator
- **THEN** neither the booking nor the environment is looked up, and nothing is pushed

### Requirement: Rows without routing metadata fall back to database authorization

When a notification lacks the routing metadata for a row it would refresh, that is, no booking owner id for the booking row, or no environment owner id for the environment row, the subscriber SHALL treat that row's routing as unknown. It SHALL NOT skip the row on that basis, and SHALL instead load and authorize the row from the database, as it did before routing metadata existed. This covers publishers running older code during a rolling deploy. Such a row is never pushed to a user who may not manage it.

#### Scenario: Legacy payload reaches the owner
- **WHEN** a connection for user U receives a notification without a booking owner id for a booking U owns
- **THEN** the booking is loaded, authorized and its rendered row is pushed

#### Scenario: Legacy payload does not reach an unrelated user
- **WHEN** a connection for ordinary user A receives a notification without a booking owner id for a booking owned by user B
- **THEN** the booking is looked up, the database-backed check rejects it, and nothing is pushed to A's connection

#### Scenario: Lifecycle notification without environment routing
- **WHEN** a connection receives a `lifecycle` notification that carries an environment id but no environment owner id
- **THEN** the environment is loaded and authorized from the database, and its rendered row is pushed only if the connection's user may manage it

### Requirement: Notifications are published only to the channels of users who may manage the row

Each row-changed notification, whether `progress` or `lifecycle`, SHALL be published to a set of scoped channels derived from its own routing metadata, not to a channel shared by every subscriber. That set SHALL contain:
- the user channel of the booking owner;
- the user channel of the booking creator, if the booking has one;
- for a `lifecycle` notification of an environment child whose environment routing is known, the user channels of the environment owner and, if it has one, the environment creator;
- the admin channel, always.

Each channel SHALL appear at most once in the set, so a user who is both owner and creator gets one copy per notification. The payload published to every channel in the set SHALL be identical, and it is the same payload as before this change. A notification SHALL NOT be published to the user channel of any user it does not name.

#### Scenario: Standalone booking of an ordinary user
- **WHEN** a standalone booking owned by user U, with no creator, changes status
- **THEN** the notification is published to U's user channel and to the admin channel, and to no other channel

#### Scenario: Booking ordered by a dispatcher
- **WHEN** dispatcher D ordered a booking on behalf of user U, and that booking records a progress line
- **THEN** the progress notification is published to U's user channel, D's user channel and the admin channel

#### Scenario: Adopted environment child with a different environment creator
- **WHEN** user U owns a namespace booking with no creator, dispatcher D ordered an environment on U's behalf that adopted it, and the booking then changes status
- **THEN** the lifecycle notification is published to U's user channel, D's user channel and the admin channel, each exactly once

#### Scenario: Progress notification for an environment child
- **WHEN** an environment child booking owned by U and created by nobody records a progress line, and its environment was ordered by dispatcher D
- **THEN** the progress notification is published to U's user channel and the admin channel only, because a progress notification never refreshes the environment row

#### Scenario: Owner and creator are the same user
- **WHEN** a booking's owner and creator are the same user U
- **THEN** the notification is published to U's user channel once

### Requirement: Notifications with unknown recipients use the broadcast channel

When a `lifecycle` notification concerns an environment child but the environment's routing could not be read at publish time, the publisher cannot tell who may manage the environment row. It SHALL then publish the notification to the broadcast channel, which every subscriber listens on, instead of the scoped channels. A notification whose recipients are fully known SHALL NOT be published to the broadcast channel.

#### Scenario: Environment routing lookup failed
- **WHEN** a lifecycle notification for an environment child is published and reading the environment's owner and creator failed or found no environment
- **THEN** the notification is published to the broadcast channel
- **AND** every connected subscriber receives it and decides per row from the routing it carries and the database, as for a payload without environment routing

#### Scenario: Recipients fully known
- **WHEN** a notification's booking routing is known and, for an environment child's lifecycle notification, its environment routing is also known
- **THEN** nothing is published to the broadcast channel

### Requirement: Subscribers listen only on their own scoped channel and the broadcast channel

A live-update subscriber SHALL listen on exactly two channels: the broadcast channel and one scoped channel chosen from its user's role when the stream is opened. That is the admin channel for an admin, and the user's own user channel for anyone else, dispatchers included. A subscriber SHALL NOT listen on any other user's channel, and a non-admin SHALL NOT listen on the admin channel. A role change SHALL take effect for a connection when the stream is next opened. The per-row routing pre-filter and the database-backed authorization still apply to every notification a subscriber receives, and the rows pushed to an authorized user SHALL be the same as before this change.

#### Scenario: Unrelated user receives nothing
- **WHEN** user A and user B each have an open stream, and a booking owned by A with no creator changes status
- **THEN** A's connection receives the notification and pushes the refreshed row
- **AND** B's connection receives no message for it at all

#### Scenario: Several tabs of the same user
- **WHEN** user U has two open streams and a booking owned by U changes status
- **THEN** each of U's connections receives the notification and pushes the refreshed row

#### Scenario: Creating dispatcher receives the row
- **WHEN** dispatcher D ordered a booking on behalf of user U, and both D and U have open streams while the booking changes status
- **THEN** both D's and U's connections receive the notification and push the refreshed row

#### Scenario: Dispatcher does not receive other users' rows
- **WHEN** dispatcher D has an open stream and a booking that D neither owns nor created changes status
- **THEN** D's connection receives no message for it

#### Scenario: Admin receives every row once
- **WHEN** an admin has an open stream and bookings of several different users change status, including one the admin owns
- **THEN** the admin's connection receives each notification exactly once and pushes each refreshed row

#### Scenario: Received row the user may not manage
- **WHEN** dispatcher D receives, on D's user channel, a lifecycle notification for an adopted environment child whose booking routing does not name D but whose environment routing names D as creator
- **THEN** the booking row is skipped without a database lookup and the environment row is loaded, authorized and pushed, as before this change

#### Scenario: Role changed while connected
- **WHEN** an ordinary user is promoted to admin while their stream is open
- **THEN** that connection keeps receiving only the user's own channel and the broadcast channel until it reconnects, and after reconnecting it receives the admin channel

### Requirement: Scoped delivery keeps the existing failure and reconnect behaviour

Scoped delivery SHALL NOT weaken the reconciliation guarantees. A subscriber whose pub/sub read fails SHALL end the stream as before, so the browser reconnects and subscribes afresh to its scoped and broadcast channels. Rows SHALL converge through page-level row reconciliation (see "List pages reconcile their displayed rows with one bounded request per interval"), which reconciles any notification lost while a connection was reconnecting or Redis was unavailable. Reconciliation SHALL NOT read from or depend on Redis. Publishing to several channels SHALL remain best-effort as a whole: a failure is logged and SHALL NOT fail or roll back the write that triggered it or interrupt a provisioning or teardown task. During a rolling deploy, a subscriber MAY miss live pushes from a publisher running a different version, but its rows SHALL converge through page-level reconciliation within the stated convergence bound, and no row SHALL be pushed over the event stream to a user who may not manage it.

#### Scenario: Notification lost during a reconnect
- **WHEN** a booking owned by U changes status while U's only stream is reconnecting
- **THEN** the change is not replayed on the new connection, and U's row reflects it within the reconciliation convergence bound for U's page

#### Scenario: Redis unavailable during a multi-channel publish
- **WHEN** Redis is unreachable while a notification for a dispatcher-ordered booking is being published to its channels
- **THEN** the error is logged, the triggering write stays committed, and the task that caused it continues

#### Scenario: Redis unavailable for longer than an interval
- **WHEN** Redis is unreachable for several reconciliation intervals while U's bookings change status
- **THEN** U's reconciliation requests keep succeeding and U's rows reflect the changes within the convergence bound, without any live push

#### Scenario: Publisher running older code during a rolling deploy
- **WHEN** a publisher still running the pre-change code publishes a notification on the broadcast channel
- **THEN** every subscriber receives it and handles it by routing and database authorization, as before this change

#### Scenario: Subscriber running older code during a rolling deploy
- **WHEN** a publisher running the new code publishes a change to a booking owned by U only to scoped channels, and U's tab is connected to a subscriber still running the pre-change code, which listens only on the broadcast channel
- **THEN** that tab receives no live push for the change
- **AND** U's row reflects the change within the reconciliation convergence bound for U's page
- **AND** no row is pushed over the event stream to any connection whose user may not manage it

### Requirement: List pages reconcile their displayed rows with one bounded request per interval

Each browser list page (the VM page, the namespace page and the environments page) SHALL replace per-row fallback polling with page-level reconciliation. A listed row SHALL NOT run a periodic request of its own.

Each list section SHALL issue at most one reconciliation request per reconciliation interval (60 seconds), whatever the number of displayed rows, appended pages or prepended rows. A list section SHALL NOT have more than one reconciliation request in flight. When a request is still outstanding at the next interval, that tick SHALL be skipped. Replacing the list section, for example by a filter change, SHALL leave exactly one reconciliation timer for the new section and none for the old one.

A reconciliation request SHALL name:
- the page's filters in effect (Mine / All, label or name, Show released)
- a batch of displayed row ids, each with the row version the page holds for it
- the list key of the newest displayed row, or none when no row is displayed

The server SHALL respond with:
- the current rendering of each requested row whose current version differs from the version sent;
- a removal directive for each requested row that no longer exists or is not visible in the page's scope;
- the new-rows indicator state;
- nothing for rows whose version is unchanged.

Reconciliation SHALL use only the database. It SHALL NOT depend on the event stream or on Redis for eventual consistency.

#### Scenario: One request per interval for 1 row
- **WHEN** a page shows one non-RELEASED booking row for ten minutes
- **THEN** the page issues at most ten reconciliation requests and no single-row refresh request

#### Scenario: One request per interval for several loaded pages
- **WHEN** a page shows three loaded pages of bookings (150 rows) for ten minutes
- **THEN** the page issues at most ten reconciliation requests in total, and no row issues a request of its own

#### Scenario: Slow response does not overlap
- **WHEN** a reconciliation request is still outstanding when the next interval elapses
- **THEN** no second reconciliation request is sent for that section until the first completes

#### Scenario: Unchanged rows are left alone
- **WHEN** none of the requested rows changed since the page rendered them
- **THEN** the response replaces no row, so an open row menu or an in-progress label edit on any of them stays as it is

### Requirement: Reconciliation requests are bounded by the server

The server SHALL enforce, independently of the client:
- at most `RECONCILE_MAX_IDS` row ids per request, a configured value no larger than the page's page size
- no duplicate id, and every id SHALL be a well-formed identifier
- every row version SHALL be a well-formed token of bounded length
- the newest-row key SHALL be a well-formed list key

A request that violates any of these SHALL be rejected with status 400 before any database work, including opening a database session and authenticating the user. A malformed request is therefore answered 400 whether or not it carries valid credentials. A request SHALL NOT be truncated silently.

A valid request SHALL execute a fixed maximum number of database statements, independent of the number of requested ids. The count includes every statement sent, including the transaction-local planner settings applied and restored around an ordered index walk. The request SHALL consist of:
- exactly one batch read of the requested rows through the list's projection
- for environments, exactly one batch read of their children
- for bookings, at most one queue-position read, and none when no requested row is queued
- exactly one newest-row probe, which reads only the newest matching row's list key in a single statement and hydrates no row
- no order-form catalog read and no per-row query

The resulting maximum is 7 statements for a bookings page and 5 for the environments page. The planner-settings statements are counted with the walks they wrap.

It SHALL return at most one rendered row or directive per requested id.

Environment children SHALL be bounded by the effective environment child limit `C_eff` defined in the environment-lifecycle requirement "Environments have a bounded number of children":
- The children read SHALL examine and return at most `C_eff + 1` children of each requested environment, and therefore at most `RECONCILE_MAX_IDS × (C_eff + 1)` children per request.
- The invariant holds once `C_eff` has been recomputed over the environments that exist: every environment that is not fully released has at most `C_eff` children. Such an environment SHALL be rendered in full, and no displayed non-RELEASED environment SHALL be excluded from reconciliation.
- A response therefore renders at most `RECONCILE_MAX_IDS` rows, each with at most `C_eff` children.
- These bounds SHALL hold for every request, without exception.
- The bounded read can return `C_eff + 1` children of an environment. That environment appeared after `C_eff` was last computed, for example one the previous app version ordered during a rolling or blue-green deploy, or a direct database edit. The server SHALL then:
  - fail closed for that environment;
  - log a warning naming it;
  - perform no further read for it;
  - ask for `C_eff` to be recomputed;
  - answer its id with a "reload required" directive instead of a rendered row.

  The directive SHALL replace the row with a bounded placeholder built only from the environment's own list fields, holding a visible "could not be refreshed — reload the page" message. It SHALL keep the row's list key, and the row SHALL stay in reconciliation. Once the recomputed `C_eff` covers the environment, a later request SHALL render it in full.
- The rest of the request SHALL be answered normally, within the same statement and child bounds.

Reconciliation SHALL require an authenticated user and SHALL refuse unauthenticated requests the same way the list pages do. It SHALL NOT be listed in the OpenAPI schema.

#### Scenario: Oversized request is rejected
- **WHEN** a client sends a reconciliation request with `RECONCILE_MAX_IDS + 1` ids
- **THEN** the server responds 400 and reads no row

#### Scenario: Duplicate or malformed id is rejected
- **WHEN** a reconciliation request repeats an id or contains a value that is not a well-formed id
- **THEN** the server responds 400 and reads no row

#### Scenario: Statement count does not grow with batch size
- **WHEN** bookings reconciliation requests are made with 1 id and with `RECONCILE_MAX_IDS` ids, each including a queued booking
- **THEN** both requests execute the same number of database statements, and neither executes more than 7

#### Scenario: Environment statement count is fixed
- **WHEN** environments reconciliation requests are made with 1 id and with `RECONCILE_MAX_IDS` ids
- **THEN** both requests execute the same number of database statements, and neither executes more than 5

#### Scenario: Environment at the child limit
- **WHEN** a displayed environment has exactly `C_eff` children and has changed
- **THEN** reconciliation renders it with all of its children
- **AND** the children read examines at most `C_eff + 1` of its children

#### Scenario: Environment over the current limit
- **WHEN** a displayed environment has `C_eff + 5` children because rows were inserted directly into the database, and its id is reconciled together with other environments
- **THEN** a warning naming the environment is logged and a recomputation of `C_eff` is requested
- **AND** its id receives the "reload required" directive, with no child rendered and no further read
- **AND** the request still executes no more than 5 statements, and reads at most `C_eff + 1` children of that environment
- **AND** the other requested environments are reconciled normally

#### Scenario: Environment ordered by the previous version during a blue-green deploy
- **WHEN** this process computed `C_eff` at startup, and the previous app version, still serving before cut-over, then ordered an environment with more children than `C_eff`
- **THEN** at most one refresh interval later `C_eff` covers it, and the environment's row is reconciled in full without a restart or page reload

#### Scenario: Malformed request with valid credentials
- **WHEN** an authenticated user sends a reconciliation request with `RECONCILE_MAX_IDS + 1` ids
- **THEN** the server responds 400 without opening a database session or looking up the user

#### Scenario: Legacy oversized environment still converges
- **WHEN** an environment ordered before the child limit existed has more children than `ENVIRONMENT_MAX_CHILDREN`, is READY and displayed, and its READY → RELEASING notification is lost during a Redis outage
- **THEN** reconciliation renders it RELEASING within the convergence bound, without a reload

#### Scenario: Unauthenticated reconciliation is refused
- **WHEN** a reconciliation request is made without an authenticated session or API key
- **THEN** the server refuses it the same way it refuses an unauthenticated list page request

### Requirement: Reconciliation authorizes every row by list visibility

Visibility (may this user see this row in this list?) SHALL be decided separately from management (may this user act on, or receive live pushes for, this row?). The server SHALL re-authorize every requested id, at request time, against the visibility rule of the requesting page and filter:
- **Mine**: the user owns the row or created it on the owner's behalf
- **All**: any row the All list of that page would show, for any authenticated user
- **every scope**: the row's kind SHALL match the page (VM and static VM on the VM page, namespace on the namespace page, environments on the environments page)

A requested id that does not exist and one that exists but is not visible SHALL receive the same removal directive. The response SHALL NOT reveal whether such an id exists.

Visible rows SHALL be rendered exactly as the list renders them for that user:
- through the list-safe projection
- with each row action offered only under its existing permission rule
- with the "Show credentials" control only under its existing visibility rule

A reconciled row SHALL NOT contain any VM password, static VM password, static VM SSH key, provisioning log body, startup script or role variable value.

#### Scenario: Forged id under Mine
- **WHEN** user U sends a Mine reconciliation request containing the id of another user's booking that U neither owns nor created
- **THEN** the response contains only a removal directive for that id, identical to the one for a nonexistent id, and no field of that booking

#### Scenario: Other users' rows on All
- **WHEN** a non-admin user viewing the All list reconciles a batch containing other users' in-flight bookings that have since changed
- **THEN** those rows are returned in their current state, without manage actions and without the "Show credentials" control, exactly as a fresh All list would show them

#### Scenario: Wrong page kind
- **WHEN** a namespace booking's id is sent in a VM-page reconciliation request
- **THEN** it receives a removal directive

#### Scenario: Dispatcher-created row under Mine
- **WHEN** a dispatcher reconciles a Mine batch containing a booking they ordered for another user
- **THEN** the row is returned with the same actions the dispatcher's Mine list shows for it

### Requirement: Every non-released row is live and converges

Every displayed booking row whose status is not RELEASED, and every displayed environment row whose derived status is not RELEASED, SHALL:
- subscribe to its live row update
- take part in reconciliation

This includes READY and FAILED rows, because they can still change, for example READY → RELEASING on TTL expiry or a release from elsewhere. A row whose rendered state is RELEASED SHALL leave both, since it can no longer change. It SHALL still carry its list key.

For a reconciled row:
- A row that has become RELEASED SHALL be rendered once in its RELEASED state, even when Show released is off. It SHALL NOT be removed for that reason.
- A row whose label or name no longer matches the label filter SHALL be rendered with its new state. It SHALL NOT be removed for that reason.
- A row SHALL be removed only when it no longer exists or is no longer visible in the page's scope.

Rows that a page shows SHALL keep their order: reconciliation SHALL replace rows in place and SHALL NOT insert, move or reorder rows. Loaded pages, the filters in effect, row actions and the next-page control with its cursor SHALL be unchanged by a reconciliation response. The list SHALL NOT be reset to its first page.

#### Scenario: READY to RELEASING without a live push
- **WHEN** a READY booking row is displayed, its TTL expires, and the event for that change is lost
- **THEN** the row shows RELEASING within the convergence bound, and later RELEASED, after which it is no longer reconciled

#### Scenario: Released row with Show released off
- **WHEN** a displayed row is released while Show released is off
- **THEN** reconciliation renders it as RELEASED in place and does not remove it

#### Scenario: Deleted row is removed
- **WHEN** an admin deletes a PENDING booking that another tab displays
- **THEN** that tab's next reconciliation of the id removes the row

#### Scenario: Reconciliation after Load more
- **WHEN** a user has loaded two pages, and a row on the second page changes
- **THEN** reconciliation updates that row in place, the second page and the next-page control stay, and no row is duplicated or skipped

### Requirement: Reconciliation states its worst-case convergence

The client SHALL choose each request's batch from the displayed non-RELEASED rows:
- **In-flight rows** (any status other than READY, FAILED and RELEASED; for environments, any derived status other than READY, FAILED and RELEASED) SHALL be taken first, in rotating order.
- **Settled rows** (READY or FAILED) SHALL fill the remaining capacity, in rotating order.
- `min(S, RECONCILE_SETTLED_MIN)` slots of each batch SHALL be reserved for settled rows, where S is the number of displayed settled rows. `RECONCILE_SETTLED_MIN` is a configured value with `1 ≤ RECONCILE_SETTLED_MIN < RECONCILE_MAX_IDS` (default 10). Whenever a settled row is displayed, at least one settled row is therefore sent in every batch, however many rows are in flight.
- Slots that neither class uses SHALL go to the other class.
- Rotation SHALL continue from where the previous batch ended. Every displayed row of a class SHALL therefore be sent once before any row of that class is sent again.

With interval T, batch size B, reserved settled share R, I in-flight rows and S settled rows, a change to a displayed row SHALL be reflected no later than the following delay after the change, plus one request's response time:
- **in-flight row**: ⌈I / (B − min(S, R))⌉ × T
- **settled row**: ⌈S / max(min(S, R), B − I)⌉ × T

Because R ≥ 1, both denominators are at least 1 whenever their class is non-empty. Every displayed non-RELEASED row therefore converges within a finite, stated delay. An environment over the current `C_eff` converges once `C_eff` has been recomputed to cover it, which happens within `ENVIRONMENT_CHILD_LIMIT_REFRESH_SECONDS`, or sooner when a request meets it. Until then it shows the "reload required" state. In particular, a row converges within one interval only while all rows of its class fit in one batch. The system SHALL NOT promise that every loaded row is refreshed every interval. Live row updates remain the fast path whenever they are delivered.

#### Scenario: First page converges every interval
- **WHEN** a page shows 50 rows (B = 50) and a row's change notification is lost
- **THEN** the row reflects the change within one interval

#### Scenario: Several loaded pages
- **WHEN** a page shows 150 rows, 5 in flight and 145 settled, with B = 50 and R = 10, and a settled row's change notification is lost
- **THEN** the row reflects the change within ⌈145 / 45⌉ = 4 intervals
- **AND** an in-flight row's lost change is reflected within one interval

#### Scenario: Settled rows are not starved by many in-flight rows
- **WHEN** a page shows 60 in-flight rows and 30 settled rows, with B = 50 and R = 10, and a settled row's change notification is lost
- **THEN** every batch contains 10 settled rows, and the row reflects the change within ⌈30 / 10⌉ = 3 intervals

#### Scenario: Settled reserve of zero is rejected
- **WHEN** the service is configured with `RECONCILE_SETTLED_MIN = 0`
- **THEN** the configuration is rejected at startup

### Requirement: Newer matching rows are signalled, not inserted

Each reconciliation response SHALL tell the page whether a matching row newer than the newest displayed row exists. This SHALL be decided by one bounded probe through the page's first-page selection for the filters in effect, including its label-scan bound.

**The newest displayed row**
- It SHALL be the first row the list section shows, whatever its status. A RELEASED row counts too, for example under Show released, or after reconciliation rendered a row as RELEASED.
- Every displayed row SHALL therefore carry its list key, including RELEASED rows.
- The request SHALL omit the newest-row key only when the section displays no row at all.
- When the page displays no row, the indicator SHALL be shown if any matching row exists.

When a newer matching row exists, the page SHALL show an indicator. Activating the indicator SHALL reload the list section with the filters in effect, as a filter change does. Reconciliation itself SHALL NOT insert rows, replace loaded pages or scan history beyond the first-page selection. Rows the user's own tab prepends, for example a newly ordered booking, SHALL count as displayed.

#### Scenario: Booking ordered in another tab
- **WHEN** user U orders a VM in one tab while another tab shows U's Mine VM list
- **THEN** the other tab's next reconciliation shows the newer-rows indicator, and the list is unchanged until U activates it

#### Scenario: Newest displayed row is RELEASED
- **WHEN** Show released is on, the first displayed row is a RELEASED booking, and no newer matching booking exists
- **THEN** the request carries that row's list key and the newer-rows indicator stays hidden

#### Scenario: First row becomes RELEASED
- **WHEN** the first displayed row is rendered as RELEASED by reconciliation while Show released is off, and no newer matching booking exists
- **THEN** later requests still carry that row's list key and the newer-rows indicator stays hidden

#### Scenario: Own order is not signalled
- **WHEN** U orders a VM from the page's form and the new row is prepended to the list
- **THEN** reconciliation does not show the newer-rows indicator for that booking

### Requirement: Obsolete reconciliation responses are ignored

A reconciliation response SHALL be applied only to the list section that sent it. A response that arrives after that section was replaced, for example by a filter change or a newer-rows reload, SHALL change nothing on the page.

A rendered row in a response SHALL NOT replace a row whose version changed after the request was sent, for example through a live row update or an action's response. Such a row is reconciled again in a later batch.

#### Scenario: Filter change while a request is in flight
- **WHEN** a user switches from All to Mine while a reconciliation request for the All section is in flight
- **THEN** that response changes no row of the new Mine section, and only the new section's timer issues further requests

#### Scenario: Live update arrives before the response
- **WHEN** a row is updated by a live row update after its reconciliation request was sent, and the response then carries an older rendering of that row
- **THEN** the row keeps the live update's rendering
