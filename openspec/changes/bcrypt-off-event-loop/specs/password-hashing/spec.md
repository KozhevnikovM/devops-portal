## Purpose

Governs how the portal hashes and verifies user passwords while serving requests. Password work must not stall unrelated requests, holds no database connection while it waits, and runs with bounded, configurable concurrency. Login and password-management behaviour stays the same.

## ADDED Requirements

### Requirement: Password work does not block request serving

Every password hash or verification done while serving a request SHALL run without blocking the process's request-serving event loop. This covers login, user creation (JSON API and admin UI), admin password reset (JSON API and admin UI) and profile password change. While such work is in flight, the same process SHALL keep making progress on other requests.

#### Scenario: Unrelated work progresses during verification
- **WHEN** a password verification is in flight
- **THEN** another task on the same event loop keeps being scheduled and making progress before the verification completes

#### Scenario: Unrelated work progresses during hashing
- **WHEN** a password hash is in flight
- **THEN** another task on the same event loop keeps being scheduled and making progress before the hash completes

#### Scenario: Unrelated request completes during an in-flight login
- **WHEN** a login request is waiting on password verification and an unrelated request arrives at the same process
- **THEN** the unrelated request completes without waiting for the login's verification to finish

### Requirement: Password work concurrency is bounded and configurable

The number of password hash or verification operations that run at the same time in one application process SHALL NOT exceed a configurable limit, `BCRYPT_MAX_CONCURRENCY`. The limit defaults to the number of CPUs available to the process. Operations beyond the limit SHALL wait for a free slot and then complete normally. They SHALL NOT be rejected. Password work SHALL NOT take capacity from the thread pool that other off-loop work in the process uses.

#### Scenario: Burst larger than the limit
- **WHEN** the limit is 2 and 6 password verifications are started at the same moment
- **THEN** no more than 2 run at any instant
- **AND** all 6 eventually complete with the correct result

#### Scenario: Default limit
- **WHEN** `BCRYPT_MAX_CONCURRENCY` is not configured
- **THEN** the limit equals the number of CPUs available to the process

#### Scenario: Invalid limit
- **WHEN** `BCRYPT_MAX_CONCURRENCY` is configured as zero or a negative number
- **THEN** the application refuses to start, reporting a configuration error

### Requirement: No database connection is held while waiting on password verification

Login and profile password change SHALL end their read-only database transaction before they wait on password verification. The pooled connection is then available to other requests for the whole verification.

#### Scenario: Login releases its connection before verifying
- **WHEN** a login has looked up the user and begins password verification
- **THEN** the request's database session holds no open transaction or checked-out connection while verification is in flight

#### Scenario: Password change releases its connection before verifying
- **WHEN** a profile password change has loaded the user and begins verifying the current password
- **THEN** the request's database session holds no open transaction or checked-out connection while verification is in flight

### Requirement: Login and password-management behaviour is preserved

Moving password work off the event loop SHALL NOT change observable behaviour. Specifically:
- A correct username and password logs the user in and sets a session cookie.
- A wrong password, or an unknown or inactive username, returns the same 401 "Invalid username or password" response.
- Login SHALL perform exactly one password verification whether or not the username exists, using a fixed dummy hash of the same cost for an unknown username, so response time does not reveal whether the account exists.
- New and reset passwords SHALL be rejected when shorter than 8 characters, with the existing messages.
- Admin password reset SHALL invalidate all of the target user's sessions. Profile password change SHALL invalidate all of the user's other sessions and keep the current one.
- Stored hashes SHALL remain standard bcrypt hashes, so existing hashes still verify and new hashes verify with any bcrypt implementation.

#### Scenario: Unknown username still pays verification cost
- **WHEN** a login is submitted for a username that does not exist
- **THEN** exactly one password verification is performed (against the dummy hash)
- **AND** the response is the same 401 as for a wrong password

#### Scenario: Existing hash still verifies
- **WHEN** a user whose password hash was created before this change logs in with the correct password
- **THEN** the login succeeds

#### Scenario: Round trip
- **WHEN** a password is hashed and then verified with the same password
- **THEN** verification succeeds
- **AND** verification with a different password fails

#### Scenario: Password change keeps current session
- **WHEN** a user changes their password from the profile page with the correct current password
- **THEN** the new password is stored, the user's other sessions are invalidated, and the current session stays valid
