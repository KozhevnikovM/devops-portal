# Bugfix: quota check races for default-quota users (#142)

**Type: Concurrency** · Source: CQ#3 · Phase 2, item #6

## Root cause

`QuotaRepository.get_limits_for_update`
([`app/infrastructure/repositories/quota_repo.py`](../../app/infrastructure/repositories/quota_repo.py))
does `SELECT … FOR UPDATE`, but a user on the **default** quota has **no `QuotaModel` row**, so
`scalar_one_or_none()` returns `None` and **locks nothing** — it just hands back the configured
defaults. Two concurrent `POST /bookings` for such a user therefore acquire no shared lock and can
both pass the quota check and exceed the limit.

A second, related gap: `CreateBookingUseCase.execute` calls `count_active_resources` **before**
`get_limits_for_update`, so even with a row the usage snapshot is read before the lock is held —
the lock can't serialize the count.

## Change

Two coordinated fixes that make the pessimistic lock actually effective:

1. **Lazy-seed the quota row, then lock it** (`get_limits_for_update`): idempotently insert the
   user's quota row from the configured defaults (`INSERT … ON CONFLICT (user_id) DO NOTHING`),
   then `SELECT … FOR UPDATE`. A row now always exists, so the row lock is always taken. Seeded
   values equal the current defaults — **no behaviour change** for limits, only that the row is
   now materialized on first booking.
2. **Lock before counting** (`CreateBookingUseCase.execute`): call `get_limits_for_update`
   (which takes the lock) **before** `count_active_resources`, so a second concurrent transaction
   blocks on the quota-row lock until the first commits and then counts the first booking.

Net effect: concurrent bookings for the same user serialize on the quota row; the usage count is
taken under the lock; the limit can't be exceeded by a race.

## Expected behaviour after the fix

- Two overlapping `POST /bookings` for a default-quota user cannot jointly exceed the limit — the
  second blocks until the first commits, then sees the updated usage.
- A `QuotaModel` row exists for the user after their first booking (seeded from defaults).
- Quota *values* are unchanged (seeded = configured defaults); single-booking behaviour is
  identical.

## Test

`tests/test_quota_race_default_users.py`:
- `get_limits_for_update` issues an `ON CONFLICT DO NOTHING` insert and then a `FOR UPDATE`
  select even when no row pre-exists (so the lock is always taken);
- `CreateBookingUseCase` acquires the lock (`get_limits_for_update`) before counting usage
  (`count_active_resources`) — call ordering asserted via a shared mock.

## Docs

Internal concurrency fix; no user-facing API change, no docs update.
