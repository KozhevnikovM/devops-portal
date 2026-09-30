## Purpose

Defines how progress output from provisioning and teardown tasks (Terraform, SSH-wait, startup-script and Ansible output) is written to a booking's status message and capped provisioning log. Writes are batched so noisy output causes a bounded number of database commits, while progress stays fresh, ordered and complete, and never overrides a lifecycle outcome.

## ADDED Requirements

### Requirement: Progress output is persisted in bounded batches

A progress producer is one provisioning or teardown task execution (one attempt) for a booking. A producer SHALL persist its progress output in batched commits, not one commit per output line. It SHALL follow these rules:

- The first progress message after a period with no persist in the last flush interval SHALL be persisted immediately.
- Messages that arrive within the flush interval after a persist SHALL be buffered and persisted together, at most once per flush interval, apart from the threshold-triggered flushes below.
- When the buffer reaches the message flush threshold or the character flush threshold, it SHALL be persisted immediately, whatever the interval.

The leading-edge and threshold rules apply only while the producer is not in retry backoff after a failed in-run flush. See "Progress persistence failures do not fail the task".

The flush interval SHALL be configurable (`PROGRESS_FLUSH_INTERVAL_MS`, default 500 ms). The message flush threshold SHALL be configurable (`PROGRESS_FLUSH_MESSAGE_THRESHOLD`, default 50). The character flush threshold SHALL be configurable (`PROGRESS_FLUSH_CHAR_THRESHOLD`, default 16,384). An interval of 0 SHALL persist every message as it arrives.

The two thresholds trigger flushes. They are not limits on the buffer's size. The only hard bound on the buffer is that its log text SHALL never exceed the 50,000-character provisioning-log cap. That bound holds in every mode, including retry backoff. Because each buffered message adds at least one newline to the log text, the cap also bounds how many messages the buffer can hold. There is no separate message-count maximum. The one item held outside that text is the pending status message, which is the single most recently recorded message.

#### Scenario: Burst of many lines under a deterministic clock
- **WHEN** a producer records N progress messages of at most 100 characters each over T seconds of clock time, with the default settings
- **THEN** the number of progress commits is at most `ceil(T / 0.5) + ceil(N / 50) + 1`, plus one final barrier flush, and not N

#### Scenario: 1,000 lines inside one interval
- **WHEN** a producer records 1,000 short progress messages without the clock advancing
- **THEN** there are at most 21 progress commits in total: one leading commit, one per 50 buffered messages, and the final barrier flush
- **AND** the provisioning log contains all 1,000 messages in order, subject to the log cap

#### Scenario: Flushing disabled
- **WHEN** the flush interval is configured as 0
- **THEN** every progress message is committed as it is recorded, as before this change

### Requirement: Progress stays fresh during normal operation

In normal operation (database reachable), a recorded progress message SHALL be persisted no later than one flush interval after it was recorded, plus the time the flush itself takes. This SHALL hold even if the producer records nothing further. A quiet period after a callback SHALL NOT leave progress buffered indefinitely.

#### Scenario: Isolated line
- **WHEN** a producer records one progress message after more than one flush interval without recording anything
- **THEN** that message is persisted immediately and becomes the booking's status message

#### Scenario: Burst followed by silence
- **WHEN** a producer records a burst of messages and then nothing for several seconds, while the task is still running (for example a long Ansible task that prints nothing)
- **THEN** the last message of the burst is persisted within one flush interval after it was recorded
- **AND** it is the booking's status message from then on, until the next progress message or lifecycle write

### Requirement: Batching preserves log content, order and the log cap

Persisting a batch SHALL leave the booking's provisioning log and status message exactly as persisting each message individually would have left them:

- The provisioning log SHALL be the previous log followed by each message and a newline, in recording order, keeping only the last 50,000 characters.
- The status message SHALL be the last message in the batch.

Messages SHALL NOT be de-duplicated, merged, split or reordered. A message that contains several lines (a multi-line output snapshot) SHALL be kept as-is. The characters buffered for the log SHALL never exceed the 50,000-character cap, since older buffered text could never survive the cap.

#### Scenario: Batched log equals per-line log
- **WHEN** the same sequence of progress messages is persisted once with batching and once with batching disabled, starting from the same log
- **THEN** the resulting provisioning logs are identical, and so are the resulting status messages

#### Scenario: Repeated multi-line snapshots
- **WHEN** a producer records two identical multi-line snapshot messages in a row
- **THEN** both appear in the provisioning log, each followed by a newline, with their embedded line breaks preserved

#### Scenario: Single message larger than the log cap
- **WHEN** a producer records one message longer than 50,000 characters
- **THEN** it is persisted immediately, and the provisioning log equals the last 50,000 characters of that message followed by its newline, i.e. the final 49,999 characters of the message and then the newline, exactly as a per-line append of `message + "\n"` followed by the tail cap would leave it
- **AND** no more than 50,000 log characters were buffered for it

#### Scenario: Continuous output over the cap
- **WHEN** a producer records far more than 50,000 characters of output
- **THEN** the provisioning log never exceeds 50,000 characters and ends with the most recently recorded output

### Requirement: Pending progress is flushed before every lifecycle write

Before a producer writes a status message outside progress output, performs a status transition, or records a controlled failure or retry, it SHALL first persist any buffered progress. The same SHALL happen before it returns early because the booking was released during provisioning, and before its attempt ends by any handled path. Lifecycle writes and their notifications SHALL remain immediate and SHALL NOT wait for a flush interval.

#### Scenario: Normal completion
- **WHEN** a provisioning attempt records progress and then marks the booking READY
- **THEN** all progress recorded before READY is in the provisioning log before the READY transition is committed

#### Scenario: Status message cleared after Terraform
- **WHEN** progress is buffered at the moment the task clears the status message after a Terraform apply or destroy
- **THEN** the buffered progress is persisted first, and the status message is cleared afterwards, not overwritten by the buffered progress

#### Scenario: Configuration failure
- **WHEN** a startup script or Ansible run fails after producing output
- **THEN** the output is persisted before the configuration-error status message and the READY transition are written

#### Scenario: Terraform or SSH failure and retry
- **WHEN** a provisioning attempt fails with an unexpected error after producing output, and the booking is moved to RETRY or FAILED
- **THEN** the output is persisted before the failure status message and the status transition are written

#### Scenario: Released during provisioning
- **WHEN** the booking is found to have been released after the Terraform apply and the task hands off to teardown and returns early
- **THEN** the progress buffered by that attempt is persisted before the task returns

### Requirement: A late progress write never overrides a lifecycle outcome

Persisting progress SHALL set the booking's status message only while the booking's status still accepts that producer's progress. For provisioning that means PROVISIONING or CONFIGURING. For teardown it means RELEASING. Otherwise the batch SHALL be appended to the provisioning log only, leaving the status message unchanged. Checking the status and writing the batch SHALL be one atomic database operation. Batches from overlapping producers for the same booking (for example teardown starting while configuration output is still streaming) SHALL each be appended in full, with no lost update.

#### Scenario: Batch lands after READY
- **WHEN** a progress batch is persisted for a booking that is already READY
- **THEN** the READY status message is unchanged and the batch is appended to the provisioning log

#### Scenario: Provisioning output after release
- **WHEN** a provisioning producer persists a batch after the booking has moved to RELEASING
- **THEN** the teardown's status message is not overwritten by provisioning output

#### Scenario: Concurrent producers append without loss
- **WHEN** a provisioning producer and a teardown producer persist batches for the same booking concurrently
- **THEN** the provisioning log contains both batches in full, each in its own recording order

### Requirement: Progress buffers are isolated per attempt

Each task execution SHALL start with an empty progress buffer. When an attempt ends by any path (success, handled failure, retry, early return), its buffer SHALL be flushed or discarded and its pending flush timer cancelled. After that it SHALL accept no further messages. Progress buffered by one attempt SHALL NOT be persisted during or after a later attempt.

#### Scenario: Retry does not replay stale progress
- **WHEN** attempt 1 records progress, fails and schedules a retry, and attempt 2 later starts
- **THEN** no progress message recorded by attempt 1 is written after attempt 1 has ended
- **AND** attempt 2's status message reflects only attempt 2's own progress and lifecycle writes

#### Scenario: Message after the attempt has ended
- **WHEN** a callback records a progress message after its attempt has ended
- **THEN** the message is not persisted and no database write occurs

### Requirement: Progress persistence failures do not fail the task

A failure to persist a progress batch SHALL be logged and SHALL NOT raise into the Terraform, SSH, startup-script or Ansible work that produced it, nor fail the attempt. Flushes fall into two classes with different failure handling:

- **In-run flushes** are the leading-edge flush of the first message after a quiet period, the trailing (interval) flush, and the threshold-triggered flush. When one of these fails, the batch SHALL remain buffered in recording order. Messages recorded afterwards SHALL be appended behind it, and the buffered log text SHALL stay bounded by the 50,000-character log cap. The retained batch SHALL be retried by a trailing flush no sooner than one flush interval after the failure, even if no further message arrives. Reaching a flush threshold SHALL NOT trigger an extra attempt before then, so an unavailable database is retried at most once per flush interval per producer.
- **Barrier flushes** are the flush before a lifecycle write and the flush at attempt end. When one of these fails, the buffered batch SHALL be discarded with a logged warning, and the lifecycle write SHALL still be attempted, so a failed flush can never be applied after the lifecycle outcome.

Publishing the progress notification SHALL happen only after a successful flush.

#### Scenario: Database briefly unavailable mid-run
- **WHEN** a periodic progress flush fails because the database is briefly unavailable, and the next flush succeeds
- **THEN** the provisioning task keeps running, the error is logged, and the next successful flush persists the retained and new messages in order

#### Scenario: Leading-edge flush fails
- **WHEN** the first message after a quiet period is persisted immediately and that persist fails
- **THEN** the message is kept in the buffer rather than dropped, the error is logged, and it is retried by a trailing flush one flush interval later, together with any messages recorded meanwhile, in order

#### Scenario: Database stays unavailable during continuous output
- **WHEN** in-run flushes keep failing while a producer records output well beyond the flush thresholds
- **THEN** persist attempts happen at most once per flush interval, and the buffered log text never exceeds 50,000 characters
- **AND** the buffer may hold more than 50 messages and more than 16,384 characters, since the thresholds are not buffer limits

#### Scenario: Barrier flush fails
- **WHEN** the flush before a READY transition fails
- **THEN** the buffered progress is discarded with a logged warning, the READY transition is still attempted, and no progress from that buffer is written afterwards

#### Scenario: No notification without a commit
- **WHEN** a progress flush fails
- **THEN** no progress row-changed notification is published for it

### Requirement: Crash loss of buffered progress is bounded and documented

A handled exception inside an attempt SHALL pass through the pre-lifecycle flush. If the worker is killed without running handlers (SIGKILL, out-of-memory kill, container or host loss), only progress recorded since the producer's last successful commit MAY be lost. The loss bound depends on how the producer was operating:

- **Normal operation**, where the producer's in-run flushes are succeeding: the lost tail SHALL be at most the output recorded within one flush interval, and below the message and character flush thresholds, since reaching either triggers a flush. The one exception is a single message larger than the character flush threshold, which is being flushed on its own and is itself capped at 50,000 characters.
- **Degraded operation**, after one or more in-run flushes have failed and their batches are being retained for retry: the lost tail is not bounded in time. It SHALL be at most 50,000 characters of provisioning-log text (the log cap that bounds the buffer), plus the pending status message.

The system SHALL NOT claim that buffered progress survives a hard kill. Both bounds SHALL be documented in the administrator guide.

#### Scenario: Worker killed mid-burst in normal operation
- **WHEN** the worker process is killed with SIGKILL while a producer whose flushes have been succeeding has buffered progress
- **THEN** the provisioning log holds everything committed before the kill, and the missing tail is at most the output of one flush interval, below the flush thresholds

#### Scenario: Worker killed while flushes are failing
- **WHEN** in-run flushes have been failing for several intervals and the worker process is then killed with SIGKILL
- **THEN** the provisioning log holds everything committed before the first failed flush, and the missing tail is at most 50,000 characters of log text

### Requirement: Persisting progress holds no database connection between flushes

Buffering progress SHALL NOT hold a database session or connection open between flushes, or during Terraform, SSH, startup-script or Ansible execution. Each flush SHALL use its own short-lived session and release it at once. The VCD API-token lock TTL SHALL still be refreshed on every progress callback, whether or not that callback triggers a flush.

#### Scenario: Idle between flushes
- **WHEN** a producer has progress buffered and is waiting for the next flush
- **THEN** it holds no database connection

#### Scenario: Token lock refreshed on a buffered callback
- **WHEN** a progress callback arrives inside a flush interval and is only buffered
- **THEN** the VCD token lock's TTL is still refreshed for that callback
