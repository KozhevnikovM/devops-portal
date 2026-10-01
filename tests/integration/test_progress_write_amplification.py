"""Integration: progress write amplification, measured on one deterministic burst (#444).

The same burst — 2,000 three-line output snapshots, the clock advancing 5 ms per line (10 s of
simulated Ansible output) — is replayed through the ProgressRecorder and the real repository
against Postgres twice: with PROGRESS_FLUSH_INTERVAL_MS=0 (one commit per line, i.e. the pre-#444
behaviour) and with the defaults. Commits are counted with an ``after_commit`` listener and the
time spent in the database is measured around each persist. Run with ``-s`` to see the numbers.
"""
import math
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.domain.booking_status import PROVISIONING_PROGRESS_STATUSES
from app.domain.constants import PERMANENT_EXPIRES_AT
from app.infrastructure.database.models import BookingModel
from app.infrastructure.progress_recorder import ProgressRecorder
from app.infrastructure.repositories import booking_repo as booking_repo_mod
from app.infrastructure.repositories.booking_repo import BookingRepository
from tests.integration._environment_lease import make_sessionmaker
from tests.test_progress_coalescing import FakeScheduler

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration, pytest.mark.asyncio(loop_scope="session")]

LINES = 2000
STEP_S = 0.005
DURATION_S = LINES * STEP_S


def _burst() -> list[str]:
    return [
        "\n".join(f"line {i - k:05d} " + "x" * 69 for k in (2, 1, 0))
        for i in range(LINES)
    ]


def _replay(Session, interval_ms: int) -> tuple[int, float, str, str]:
    booking_id = uuid4()
    with Session() as s:
        s.add(BookingModel(
            id=booking_id, user_id="inttest-owner", status="PROVISIONING", resource_type="VM",
            ttl_minutes=60, expires_at=PERMANENT_EXPIRES_AT, created_at=datetime.now(timezone.utc),
        ))
        s.commit()
    commits = 0
    db_time = 0.0

    def _count(_session):
        nonlocal commits
        commits += 1

    def persist(chunk: str, last: str) -> None:
        nonlocal db_time
        start = time.perf_counter()
        with Session() as s:
            BookingRepository().sync_append_progress(s, booking_id, chunk, last, PROVISIONING_PROGRESS_STATUSES)
        db_time += time.perf_counter() - start

    sched = FakeScheduler()
    recorder = ProgressRecorder(
        persist, interval_s=interval_ms / 1000,
        message_threshold=settings.PROGRESS_FLUSH_MESSAGE_THRESHOLD,
        char_threshold=settings.PROGRESS_FLUSH_CHAR_THRESHOLD,
        clock=lambda: sched.now, timer_factory=sched,
    )
    event.listen(Session, "after_commit", _count)
    try:
        for msg in _burst():
            recorder.record(msg)
            sched.advance(STEP_S)
        recorder.close()  # the final barrier flush
    finally:
        event.remove(Session, "after_commit", _count)
    with Session() as s:
        model = s.get(BookingModel, booking_id)
        log, message = model.provisioning_log, model.status_message
        s.execute(delete(BookingModel).where(BookingModel.id == booking_id))
        s.commit()
    return commits, db_time, log, message


async def test_batching_bounds_commits_on_the_same_burst(async_engine: AsyncEngine, monkeypatch):
    monkeypatch.setattr(booking_repo_mod, "publish_progress_changed", lambda **_: None)
    Session = make_sessionmaker()

    per_line_commits, per_line_time, per_line_log, per_line_msg = _replay(Session, 0)
    batched_commits, batched_time, batched_log, batched_msg = _replay(
        Session, settings.PROGRESS_FLUSH_INTERVAL_MS,
    )
    print(
        f"\nper-line (interval 0): commits={per_line_commits} db_time={per_line_time:.3f}s"
        f"\nbatched (defaults):    commits={batched_commits} db_time={batched_time:.3f}s"
    )

    assert per_line_commits == LINES
    interval_s = settings.PROGRESS_FLUSH_INTERVAL_MS / 1000
    bound = math.ceil(DURATION_S / interval_s) + math.ceil(LINES / settings.PROGRESS_FLUSH_MESSAGE_THRESHOLD) + 2
    assert batched_commits <= bound
    assert (batched_log, batched_msg) == (per_line_log, per_line_msg)
