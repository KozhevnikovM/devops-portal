"""Integration: the atomic progress append against real Postgres (#444).

``sync_append_progress`` appends a batch to the capped provisioning_log and sets status_message
only while the booking's status accepts that producer's progress — all in one UPDATE, so a late
batch can't overwrite a lifecycle outcome and overlapping producers can't lose each other's lines.
"""
import threading
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.booking_status import (
    PROVISIONING_PROGRESS_STATUSES,
    TEARDOWN_PROGRESS_STATUSES,
)
from app.domain.constants import PERMANENT_EXPIRES_AT, PROVISIONING_LOG_MAX_CHARS
from app.infrastructure.database.models import BookingModel
from app.infrastructure.repositories import booking_repo as booking_repo_mod
from app.infrastructure.repositories.booking_repo import BookingRepository
from tests.integration._environment_lease import make_sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration, pytest.mark.asyncio(loop_scope="session")]

CAP = PROVISIONING_LOG_MAX_CHARS


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(booking_repo_mod, "publish_progress_changed", lambda **_: None)
    Session = make_sessionmaker()
    created = []

    def insert(status="PROVISIONING", log=None, message=None):
        booking_id = uuid4()
        with Session() as s:
            s.add(BookingModel(
                id=booking_id, user_id="inttest-owner", status=status, resource_type="VM",
                ttl_minutes=60, expires_at=PERMANENT_EXPIRES_AT, created_at=datetime.now(timezone.utc),
                provisioning_log=log, status_message=message,
            ))
            s.commit()
        created.append(booking_id)
        return booking_id

    def read(booking_id):
        with Session() as s:
            m = s.get(BookingModel, booking_id)
            return m.provisioning_log, m.status_message

    def append(booking_id, messages, accepting=PROVISIONING_PROGRESS_STATUSES):
        chunk = "".join(m + "\n" for m in messages)[-CAP:]
        with Session() as s:
            BookingRepository().sync_append_progress(s, booking_id, chunk, messages[-1][-CAP:], accepting)

    yield Session, insert, read, append
    with Session() as s:
        s.execute(delete(BookingModel).where(BookingModel.id.in_(created)))
        s.commit()


def _per_line(log, messages):
    for m in messages:
        log = (log + m + "\n")[-CAP:]
    return log


async def test_append_keeps_the_tail_cap(async_engine: AsyncEngine, db):
    _, insert, read, append = db
    existing = "HEAD" + "x" * (CAP - 8) + "TAIL"
    booking_id = insert(log=existing)
    append(booking_id, ["new-line"])
    log, message = read(booking_id)
    assert len(log) == CAP
    assert log.endswith("TAIL" + "new-line\n")
    assert "HEAD" not in log
    assert message == "new-line"


async def test_batch_equals_per_line_result(async_engine: AsyncEngine, db):
    _, insert, read, append = db
    messages = [f"TASK {i}\nok: [host]\n" + "y" * (i % 300) for i in range(600)]
    batched = insert(log="prior\n")
    per_line = insert(log="prior\n")
    for i in range(0, len(messages), 50):
        append(batched, messages[i:i + 50])
    for m in messages:
        append(per_line, [m])
    assert read(batched) == read(per_line)
    assert read(batched)[0] == _per_line("prior\n", messages)


async def test_oversized_message_is_the_exact_capped_append(async_engine: AsyncEngine, db):
    _, insert, read, append = db
    booking_id = insert()
    huge = "".join(chr(97 + i % 26) for i in range(CAP + 1234))
    append(booking_id, [huge])
    log, message = read(booking_id)
    assert log == huge[-(CAP - 1):] + "\n"
    assert message == huge[-CAP:]


@pytest.mark.parametrize("accepting,status,sets_message", [
    (PROVISIONING_PROGRESS_STATUSES, "PROVISIONING", True),
    (PROVISIONING_PROGRESS_STATUSES, "CONFIGURING", True),
    (PROVISIONING_PROGRESS_STATUSES, "READY", False),
    (PROVISIONING_PROGRESS_STATUSES, "FAILED", False),
    (PROVISIONING_PROGRESS_STATUSES, "RETRY", False),
    (PROVISIONING_PROGRESS_STATUSES, "RELEASING", False),
    (TEARDOWN_PROGRESS_STATUSES, "RELEASING", True),
    (TEARDOWN_PROGRESS_STATUSES, "RELEASED", False),
    (TEARDOWN_PROGRESS_STATUSES, "FAILED", False),
    (TEARDOWN_PROGRESS_STATUSES, "PROVISIONING", False),
])
async def test_status_guard(async_engine: AsyncEngine, db, accepting, status, sets_message):
    _, insert, read, append = db
    booking_id = insert(status=status, log="before\n", message="lifecycle outcome")
    append(booking_id, ["late progress"], accepting)
    log, message = read(booking_id)
    assert log == "before\nlate progress\n"   # the log append is unconditional
    assert message == ("late progress" if sets_message else "lifecycle outcome")


async def test_concurrent_producers_append_without_loss(async_engine: AsyncEngine, db):
    _, insert, read, append = db
    booking_id = insert(status="RELEASING")
    barrier = threading.Barrier(2)
    errors = []

    def producer(name, accepting):
        try:
            barrier.wait()
            for i in range(0, 200, 5):
                append(booking_id, [f"{name}-{j:03d}" for j in range(i, i + 5)], accepting)
        except Exception as exc:  # noqa: BLE001 — any error in the thread is surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=producer, args=("prov", PROVISIONING_PROGRESS_STATUSES)),
        threading.Thread(target=producer, args=("tear", TEARDOWN_PROGRESS_STATUSES)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    lines = read(booking_id)[0].splitlines()
    for name in ("prov", "tear"):
        mine = [line for line in lines if line.startswith(name)]
        assert mine == [f"{name}-{j:03d}" for j in range(200)]   # all present, in order
    assert read(booking_id)[1] == "tear-199"                     # only teardown's set it (RELEASING)
