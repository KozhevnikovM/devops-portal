"""Integration: Scarce pooled resource allocation and release concurrency tests (#452).

Exercises race conditions across independent PostgreSQL sessions and row locks:
1. Concurrent any-available allocation of the last free pooled resource (Namespace & StaticVM).
2. Concurrent specific allocation of the same pooled resource (Namespace & StaticVM).
3. Concurrent worker queue promotions (Namespace & StaticVM).
4. Concurrent environment release calls with mixed children (VM & Pooled).
5. Concurrent release calls with overlapping reads (atomic claim / no duplicate teardown).
6. Allocation racing with release of the sole held pooled resource (Namespace & StaticVM).
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.application.ports import TaskDispatcher
from app.application.use_cases.book_namespace import BookNamespaceUseCase
from app.application.use_cases.release_booking import ReleaseBookingUseCase
from app.application.use_cases.release_environment import ReleaseEnvironmentUseCase
from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase
from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.environment_status import derive_environment_status
from app.domain.exceptions import NamespaceUnavailableError, StaticVMUnavailableError
from app.infrastructure.database.models import (
    BookingAuditModel,
    BookingModel,
    EnvironmentModel,
    NamespaceModel,
    StaticVMModel,
    UserModel,
)
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres_integration,
    pytest.mark.asyncio(loop_scope="session"),
]

_TIMEOUT = 5.0


async def _assert_distinct_backends(
    session1: AsyncSession, session2: AsyncSession
) -> tuple[int, int]:
    pid1 = (await session1.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    pid2 = (await session2.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    assert pid1 != pid2, f"Expected distinct backend PIDs, got shared PID {pid1}"
    return pid1, pid2


@asynccontextmanager
async def _isolate_scarce_resource(
    engine: AsyncEngine, resource_type: ResourceType, keep_id: UUID
) -> AsyncGenerator[None, None]:
    """Temporarily deactivate all other resources of resource_type so keep_id is the sole resource in the pool."""
    model_cls = (
        NamespaceModel if resource_type == ResourceType.NAMESPACE else StaticVMModel
    )
    deactivated_ids: list[UUID] = []
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.execute(
            select(model_cls.id).where(
                model_cls.id != keep_id,
                model_cls.is_active.is_(True),
            )
        )
        deactivated_ids = list(result.scalars().all())
        if deactivated_ids:
            await session.execute(
                update(model_cls)
                .where(model_cls.id.in_(deactivated_ids))
                .values(is_active=False)
            )
            await session.commit()

    try:
        yield
    finally:
        if deactivated_ids:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await session.execute(
                    update(model_cls)
                    .where(model_cls.id.in_(deactivated_ids))
                    .values(is_active=True)
                )
                await session.commit()


@asynccontextmanager
async def _isolate_queued_bookings(
    engine: AsyncEngine, resource_type: ResourceType, test_booking_ids: set[UUID]
) -> AsyncGenerator[None, None]:
    """Temporarily shift pre-existing QUEUED bookings away so test_booking_ids are the only candidates."""
    shifted_ids: list[UUID] = []
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.execute(
            select(BookingModel.id).where(
                BookingModel.status == BookingStatus.QUEUED.value,
                BookingModel.resource_type == resource_type.value,
                BookingModel.id.not_in(test_booking_ids),
            )
        )
        shifted_ids = list(result.scalars().all())
        if shifted_ids:
            await session.execute(
                update(BookingModel)
                .where(BookingModel.id.in_(shifted_ids))
                .values(status=BookingStatus.PENDING.value)
            )
            await session.commit()

    try:
        yield
    finally:
        if shifted_ids:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await session.execute(
                    update(BookingModel)
                    .where(BookingModel.id.in_(shifted_ids))
                    .values(status=BookingStatus.QUEUED.value)
                )
                await session.commit()


async def _seed_user(engine: AsyncEngine) -> User:
    user_id = uuid4()
    now = datetime.now(UTC)
    user = User(
        id=user_id,
        username=f"user-{user_id.hex[:8]}",
        password_hash="unused",
        role="user",
        is_active=True,
        created_at=now,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            UserModel(
                id=user.id,
                username=user.username,
                password_hash=user.password_hash,
                role=user.role,
                is_active=user.is_active,
                created_at=user.created_at,
            )
        )
        await session.commit()
    return user


async def _seed_namespace(engine: AsyncEngine) -> UUID:
    ns_id = uuid4()
    now = datetime.now(UTC)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            NamespaceModel(
                id=ns_id,
                name=f"ns-{ns_id.hex[:8]}",
                cluster_name=f"cluster-{ns_id.hex[:8]}",
                api_url=None,
                is_active=True,
                created_at=now,
            )
        )
        await session.commit()
    return ns_id


async def _seed_static_vm(engine: AsyncEngine) -> UUID:
    vm_id = uuid4()
    now = datetime.now(UTC)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            StaticVMModel(
                id=vm_id,
                name=f"svm-{vm_id.hex[:8]}",
                host=f"10.0.1.{vm_id.int % 250 + 1}",
                username="root",
                password="secret-password",
                ssh_key=None,
                cpus=2,
                memory_mb=2048,
                is_active=True,
                created_at=now,
            )
        )
        await session.commit()
    return vm_id


async def _cleanup(
    engine: AsyncEngine,
    *,
    booking_ids: list[UUID] | None = None,
    namespace_ids: list[UUID] | None = None,
    static_vm_ids: list[UUID] | None = None,
    environment_ids: list[UUID] | None = None,
    user_ids: list[UUID] | None = None,
) -> None:
    async with AsyncSession(engine) as session:
        if booking_ids:
            await session.execute(
                delete(BookingAuditModel).where(
                    BookingAuditModel.booking_id.in_(booking_ids)
                )
            )
            await session.execute(
                delete(BookingModel).where(BookingModel.id.in_(booking_ids))
            )
        if environment_ids:
            await session.execute(
                delete(BookingAuditModel).where(
                    BookingAuditModel.booking_id.in_(
                        select(BookingModel.id).where(
                            BookingModel.environment_id.in_(environment_ids)
                        )
                    )
                )
            )
            await session.execute(
                delete(BookingModel).where(
                    BookingModel.environment_id.in_(environment_ids)
                )
            )
            await session.execute(
                delete(EnvironmentModel).where(EnvironmentModel.id.in_(environment_ids))
            )
        if namespace_ids:
            await session.execute(
                delete(NamespaceModel).where(NamespaceModel.id.in_(namespace_ids))
            )
        if static_vm_ids:
            await session.execute(
                delete(StaticVMModel).where(StaticVMModel.id.in_(static_vm_ids))
            )
        if user_ids:
            await session.execute(delete(UserModel).where(UserModel.id.in_(user_ids)))
        await session.commit()


# ==============================================================================
# Task 2.1: Concurrent allocation of last free pooled resource (any-available)
# ==============================================================================


@pytest.mark.parametrize(
    "resource_type", [ResourceType.NAMESPACE, ResourceType.STATIC_VM]
)
async def test_concurrent_allocation_last_free_resource(
    async_engine: AsyncEngine, resource_type: ResourceType
):
    """Two concurrent any-available requests race for the last free pooled resource.

    Row locking (FOR UPDATE SKIP LOCKED) ensures exactly one acquires the resource
    (READY with FK set), and the other enqueues (QUEUED with FK None).
    """
    user = await _seed_user(async_engine)
    created_bookings: list[UUID] = []

    if resource_type == ResourceType.NAMESPACE:
        res_id = await _seed_namespace(async_engine)
        ns_ids = [res_id]
        svm_ids = []
        pool_repo = NamespaceRepository()
        fk_field = "namespace_id"
    else:
        res_id = await _seed_static_vm(async_engine)
        ns_ids = []
        svm_ids = [res_id]
        pool_repo = StaticVMRepository()
        fk_field = "static_vm_id"

    try:
        async with _isolate_scarce_resource(async_engine, resource_type, res_id):
            s1_locked = asyncio.Event()
            s2_evaluated = asyncio.Event()

            # Wrap lock_next_available for session 1 so it holds the lock while session 2 evaluates
            orig_lock_next = pool_repo.lock_next_available

            async def locking_wrapper_s1(sess):
                res = await orig_lock_next(sess)
                s1_locked.set()
                # Wait for session 2 to complete its attempt while session 1 holds lock
                await asyncio.wait_for(s2_evaluated.wait(), timeout=_TIMEOUT)
                return res

            async def run_op1():
                async with AsyncSession(
                    async_engine, expire_on_commit=False
                ) as session1:
                    pid1 = (
                        await session1.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    # Use custom pool repo with wrapper for session 1
                    custom_pool = (
                        NamespaceRepository()
                        if resource_type == ResourceType.NAMESPACE
                        else StaticVMRepository()
                    )
                    custom_pool.lock_next_available = locking_wrapper_s1
                    if resource_type == ResourceType.NAMESPACE:
                        uc1 = BookNamespaceUseCase(BookingRepository(), custom_pool)
                    else:
                        uc1 = ReserveStaticVMUseCase(BookingRepository(), custom_pool)
                    b1 = await uc1.execute(
                        session1, ttl_minutes=60, user_id=str(user.id)
                    )
                    return pid1, b1

            async def run_op2():
                async with AsyncSession(
                    async_engine, expire_on_commit=False
                ) as session2:
                    pid2 = (
                        await session2.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    # Wait until session 1 has locked the free resource
                    await asyncio.wait_for(s1_locked.wait(), timeout=_TIMEOUT)
                    try:
                        if resource_type == ResourceType.NAMESPACE:
                            uc2 = BookNamespaceUseCase(
                                BookingRepository(), NamespaceRepository()
                            )
                        else:
                            uc2 = ReserveStaticVMUseCase(
                                BookingRepository(), StaticVMRepository()
                            )
                        b2 = await uc2.execute(
                            session2, ttl_minutes=60, user_id=str(user.id)
                        )
                        return pid2, b2
                    finally:
                        s2_evaluated.set()

            results = await asyncio.gather(run_op1(), run_op2())
            (pid1, b1), (pid2, b2) = results
            assert pid1 != pid2, f"Sessions shared PID: {pid1} == {pid2}"

            created_bookings.extend([b1.id, b2.id])
            bookings = [b1, b2]

        ready_bookings = [b for b in bookings if b.status == BookingStatus.READY]
        queued_bookings = [b for b in bookings if b.status == BookingStatus.QUEUED]

        assert len(ready_bookings) == 1, (
            f"Expected 1 READY booking, got {len(ready_bookings)}"
        )
        assert len(queued_bookings) == 1, (
            f"Expected 1 QUEUED booking, got {len(queued_bookings)}"
        )

        winner = ready_bookings[0]
        loser = queued_bookings[0]

        assert getattr(winner, fk_field) == res_id
        assert getattr(loser, fk_field) is None

        # Verify database state: exactly one booking holds the resource
        async with AsyncSession(async_engine, expire_on_commit=False) as check_sess:
            stmt = select(BookingModel).where(
                BookingModel.status.in_(["READY", "PROVISIONING", "CONFIGURING"]),
                getattr(BookingModel, fk_field) == res_id,
            )
            rows = (await check_sess.execute(stmt)).scalars().all()
            assert len(rows) == 1
            assert rows[0].id == winner.id
    finally:
        await _cleanup(
            async_engine,
            booking_ids=created_bookings,
            namespace_ids=ns_ids,
            static_vm_ids=svm_ids,
            user_ids=[user.id],
        )


# ==============================================================================
# Task 2.2: Concurrent specific allocation of the same pooled resource
# ==============================================================================


@pytest.mark.parametrize(
    "resource_type", [ResourceType.NAMESPACE, ResourceType.STATIC_VM]
)
async def test_concurrent_allocation_specific_resource(
    async_engine: AsyncEngine, resource_type: ResourceType
):
    """Two concurrent specific-allocation requests race for the same active resource.

    SELECT ... FOR UPDATE locks the chosen resource; exactly one request succeeds (READY),
    while the other unblocks after commit, observes the resource held, and raises an unavailable error.
    """
    user = await _seed_user(async_engine)
    created_bookings: list[UUID] = []

    if resource_type == ResourceType.NAMESPACE:
        res_id = await _seed_namespace(async_engine)
        ns_ids = [res_id]
        svm_ids = []
        unavailable_exc = NamespaceUnavailableError
        fk_field = "namespace_id"
    else:
        res_id = await _seed_static_vm(async_engine)
        ns_ids = []
        svm_ids = [res_id]
        unavailable_exc = StaticVMUnavailableError
        fk_field = "static_vm_id"

    try:
        s1_locked = asyncio.Event()
        s2_started = asyncio.Event()

        orig_lock_for_alloc = (
            NamespaceRepository().lock_for_allocation
            if resource_type == ResourceType.NAMESPACE
            else StaticVMRepository().lock_for_allocation
        )

        async def locking_wrapper_s1(sess, r_id):
            res = await orig_lock_for_alloc(sess, r_id)
            s1_locked.set()
            # Wait until session 2 starts trying to acquire the lock
            await asyncio.wait_for(s2_started.wait(), timeout=_TIMEOUT)
            # Give session 2 a moment to block on the postgres row lock
            await asyncio.sleep(0.05)
            return res

        async def run_op1():
            async with AsyncSession(async_engine, expire_on_commit=False) as session1:
                pid1 = (
                    await session1.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
                custom_pool = (
                    NamespaceRepository()
                    if resource_type == ResourceType.NAMESPACE
                    else StaticVMRepository()
                )
                custom_pool.lock_for_allocation = locking_wrapper_s1
                if resource_type == ResourceType.NAMESPACE:
                    uc1 = BookNamespaceUseCase(BookingRepository(), custom_pool)
                    b1 = await uc1.execute(
                        session1,
                        ttl_minutes=60,
                        user_id=str(user.id),
                        namespace_id=res_id,
                    )
                else:
                    uc1 = ReserveStaticVMUseCase(BookingRepository(), custom_pool)
                    b1 = await uc1.execute(
                        session1,
                        ttl_minutes=60,
                        user_id=str(user.id),
                        static_vm_id=res_id,
                    )
                return pid1, b1

        async def run_op2():
            async with AsyncSession(async_engine, expire_on_commit=False) as session2:
                pid2 = (
                    await session2.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
                await asyncio.wait_for(s1_locked.wait(), timeout=_TIMEOUT)
                s2_started.set()
                if resource_type == ResourceType.NAMESPACE:
                    uc2 = BookNamespaceUseCase(
                        BookingRepository(), NamespaceRepository()
                    )
                    b2 = await uc2.execute(
                        session2,
                        ttl_minutes=60,
                        user_id=str(user.id),
                        namespace_id=res_id,
                    )
                else:
                    uc2 = ReserveStaticVMUseCase(
                        BookingRepository(), StaticVMRepository()
                    )
                    b2 = await uc2.execute(
                        session2,
                        ttl_minutes=60,
                        user_id=str(user.id),
                        static_vm_id=res_id,
                    )
                return pid2, b2

        results = await asyncio.gather(run_op1(), run_op2(), return_exceptions=True)

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, unavailable_exc)]
        unexpected_errors = [
            r
            for r in results
            if isinstance(r, Exception) and not isinstance(r, unavailable_exc)
        ]

        assert unexpected_errors == [], f"Unexpected exceptions: {unexpected_errors}"
        assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
        assert len(failures) == 1, (
            f"Expected 1 {unavailable_exc.__name__}, got {len(failures)}"
        )

        _pid1, winner = successes[0]
        created_bookings.append(winner.id)

        assert winner.status == BookingStatus.READY
        assert getattr(winner, fk_field) == res_id

        # Verify DB single ownership
        async with AsyncSession(async_engine, expire_on_commit=False) as check_sess:
            stmt = select(BookingModel).where(
                BookingModel.status == "READY",
                getattr(BookingModel, fk_field) == res_id,
            )
            rows = (await check_sess.execute(stmt)).scalars().all()
            assert len(rows) == 1
    finally:
        await _cleanup(
            async_engine,
            booking_ids=created_bookings,
            namespace_ids=ns_ids,
            static_vm_ids=svm_ids,
            user_ids=[user.id],
        )


# ==============================================================================
# Task 3.1: Concurrent worker queue promotions racing over a single free resource
# ==============================================================================


@pytest.mark.parametrize(
    "resource_type", [ResourceType.NAMESPACE, ResourceType.STATIC_VM]
)
async def test_concurrent_worker_queue_promotion(
    async_engine: AsyncEngine, resource_type: ResourceType
):
    """Two concurrent workers run promote_next_queued for the same resource type.

    Row locking (FOR UPDATE SKIP LOCKED) ensures exactly one queued booking is promoted to READY,
    while the other worker returns None.
    """
    user = await _seed_user(async_engine)
    created_bookings: list[UUID] = []

    if resource_type == ResourceType.NAMESPACE:
        res_id = await _seed_namespace(async_engine)
        ns_ids = [res_id]
        svm_ids = []
    else:
        res_id = await _seed_static_vm(async_engine)
        ns_ids = []
        svm_ids = [res_id]

    # Seed two QUEUED bookings
    now = datetime.now(UTC)
    q1_id = uuid4()
    q2_id = uuid4()
    async with AsyncSession(async_engine, expire_on_commit=False) as session:
        session.add(
            BookingModel(
                id=q1_id,
                user_id=str(user.id),
                status=BookingStatus.QUEUED.value,
                resource_type=resource_type.value,
                ttl_minutes=60,
                expires_at=now + timedelta(hours=1),
                created_at=now,
                cpus=0,
                memory_mb=0,
                disk_mb=0,
            )
        )
        session.add(
            BookingModel(
                id=q2_id,
                user_id=str(user.id),
                status=BookingStatus.QUEUED.value,
                resource_type=resource_type.value,
                ttl_minutes=60,
                expires_at=now + timedelta(hours=1),
                created_at=now + timedelta(seconds=1),
                cpus=0,
                memory_mb=0,
                disk_mb=0,
            )
        )
        await session.commit()
    created_bookings.extend([q1_id, q2_id])

    try:
        async with (
            _isolate_scarce_resource(async_engine, resource_type, res_id),
            _isolate_queued_bookings(async_engine, resource_type, {q1_id, q2_id}),
        ):
            repo = BookingRepository()
            w1_locked = asyncio.Event()
            w2_done = asyncio.Event()

            orig_promote = repo.promote_next_queued

            async def run_worker1():
                async with AsyncSession(
                    async_engine, expire_on_commit=False
                ) as session1:
                    pid1 = (
                        await session1.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    orig_commit = session1.commit

                    async def hooked_commit():
                        w1_locked.set()
                        await asyncio.wait_for(w2_done.wait(), timeout=_TIMEOUT)
                        await orig_commit()

                    session1.commit = hooked_commit
                    res = await orig_promote(session1, resource_type.value)
                    return pid1, res

            async def run_worker2():
                async with AsyncSession(
                    async_engine, expire_on_commit=False
                ) as session2:
                    pid2 = (
                        await session2.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    # Wait until worker 1 has acquired row locks and is paused in pre-commit
                    await asyncio.wait_for(w1_locked.wait(), timeout=_TIMEOUT)
                    try:
                        res = await orig_promote(session2, resource_type.value)
                        return pid2, res
                    finally:
                        w2_done.set()

            # Run concurrent promotions
            results = await asyncio.gather(
                run_worker1(), run_worker2(), return_exceptions=True
            )
            unexpected = [r for r in results if isinstance(r, Exception)]
            assert unexpected == [], f"Unexpected errors: {unexpected}"

            (pid1, res1), (pid2, res2) = results
            assert pid1 != pid2

            promoted = [r for r in [res1, res2] if isinstance(r, Booking)]
            none_results = [r for r in [res1, res2] if r is None]

            assert len(promoted) == 1, (
                f"Expected 1 promoted booking, got {len(promoted)}"
            )
            assert len(none_results) == 1, (
                f"Expected 1 None result, got {len(none_results)}"
            )

            promoted_booking = promoted[0]
            assert promoted_booking.status == BookingStatus.READY
            assert promoted_booking.id == q1_id  # Oldest queued booking promoted FIFO

            # Verify DB: exactly one READY booking holding the resource
            async with AsyncSession(async_engine, expire_on_commit=False) as check_sess:
                q1_db = (
                    await check_sess.execute(
                        select(BookingModel).where(BookingModel.id == q1_id)
                    )
                ).scalar_one()
                q2_db = (
                    await check_sess.execute(
                        select(BookingModel).where(BookingModel.id == q2_id)
                    )
                ).scalar_one()
                assert q1_db.status == BookingStatus.READY.value
                assert q2_db.status == BookingStatus.QUEUED.value
    finally:
        await _cleanup(
            async_engine,
            booking_ids=created_bookings,
            namespace_ids=ns_ids,
            static_vm_ids=svm_ids,
            user_ids=[user.id],
        )


# ==============================================================================
# Task 4.1: Concurrent environment release calls with mixed children
# ==============================================================================


async def test_concurrent_environment_release(async_engine: AsyncEngine):
    """Two concurrent ReleaseEnvironmentUseCase calls race on the same environment.

    Verifies:
    - Phase 1 (Immediate return): Both calls succeed without error. VM child reaches RELEASING
      with exactly one teardown dispatch. Pooled child reaches RELEASED with exactly one promote.
    - Phase 2 (Convergence): Dispatched teardown execution transitions VM child to RELEASED.
      Parent environment status derives RELEASED.
    """
    user = await _seed_user(async_engine)
    ns_id = await _seed_namespace(async_engine)
    env_id = uuid4()
    vm_child_id = uuid4()
    pooled_child_id = uuid4()

    now = datetime.now(UTC)
    async with AsyncSession(async_engine, expire_on_commit=False) as session:
        session.add(
            EnvironmentModel(
                id=env_id,
                name=f"env-{env_id.hex[:8]}",
                blueprint_name="mixed-stack",
                user_id=str(user.id),
                ttl_minutes=120,
                expires_at=now + timedelta(hours=2),
                construction_complete=True,
            )
        )
        await session.flush()
        session.add(
            BookingModel(
                id=vm_child_id,
                user_id=str(user.id),
                status=BookingStatus.READY.value,
                resource_type=ResourceType.VM.value,
                ttl_minutes=120,
                expires_at=now + timedelta(hours=2),
                environment_id=env_id,
                created_at=now,
                cpus=2,
                memory_mb=2048,
                disk_mb=10240,
            )
        )
        session.add(
            BookingModel(
                id=pooled_child_id,
                user_id=str(user.id),
                status=BookingStatus.READY.value,
                resource_type=ResourceType.NAMESPACE.value,
                namespace_id=ns_id,
                ttl_minutes=120,
                expires_at=now + timedelta(hours=2),
                environment_id=env_id,
                created_at=now,
                cpus=0,
                memory_mb=0,
                disk_mb=0,
            )
        )
        await session.commit()

    created_bookings = [vm_child_id, pooled_child_id]

    try:
        async with _isolate_scarce_resource(
            async_engine, ResourceType.NAMESPACE, ns_id
        ):
            dispatcher = MagicMock(spec=TaskDispatcher)
            booking_repo = BookingRepository()
            orig_promote = booking_repo.promote_next_queued
            promote_spy = AsyncMock(wraps=orig_promote)
            booking_repo.promote_next_queued = promote_spy
            env_repo = EnvironmentRepository()
            release_booking_uc = ReleaseBookingUseCase(booking_repo, dispatcher)

            barrier = asyncio.Barrier(2)

            async def run_env_release():
                async with AsyncSession(
                    async_engine, expire_on_commit=False
                ) as session:
                    pid = (
                        await session.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    await asyncio.wait_for(barrier.wait(), timeout=_TIMEOUT)
                    uc = ReleaseEnvironmentUseCase(env_repo, release_booking_uc)
                    res = await uc.execute(session, env_id, user)
                    return pid, res

            results = await asyncio.gather(
                run_env_release(), run_env_release(), return_exceptions=True
            )
            unexpected = [r for r in results if isinstance(r, Exception)]
            assert unexpected == [], (
                f"Unexpected error during concurrent environment release: {unexpected}"
            )

            (pid1, env1), (pid2, env2) = results
            assert pid1 != pid2

            # Phase 1 Assertions:
            assert env1.id == env_id
            assert env2.id == env_id

            # Dispatch teardown must have been called EXACTLY ONCE for the VM child
            dispatcher.dispatch_teardown.assert_called_once_with(
                str(vm_child_id), request_id=None
            )

            # Queue promotion must have been emitted EXACTLY ONCE for the pooled child
            promote_spy.assert_awaited_once_with(ANY, ResourceType.NAMESPACE.value)

            async with AsyncSession(async_engine, expire_on_commit=False) as check_sess:
                vm_db = (
                    await check_sess.execute(
                        select(BookingModel).where(BookingModel.id == vm_child_id)
                    )
                ).scalar_one()
                pooled_db = (
                    await check_sess.execute(
                        select(BookingModel).where(BookingModel.id == pooled_child_id)
                    )
                ).scalar_one()
                assert vm_db.status == BookingStatus.RELEASING.value
                assert pooled_db.status == BookingStatus.RELEASED.value

            # Phase 2 Assertions (Convergence):
            # Simulate VM teardown completion
            async with AsyncSession(
                async_engine, expire_on_commit=False
            ) as teardown_sess:
                await booking_repo.update_status(
                    teardown_sess, vm_child_id, BookingStatus.RELEASED
                )

            async with AsyncSession(async_engine, expire_on_commit=False) as final_sess:
                final_env = await env_repo.get(final_sess, env_id)
                assert (
                    derive_environment_status(b.status for b in final_env.bookings)
                    == BookingStatus.RELEASED
                )
                for child in final_env.bookings:
                    assert child.status == BookingStatus.RELEASED
    finally:
        await _cleanup(
            async_engine,
            booking_ids=created_bookings,
            namespace_ids=[ns_id],
            environment_ids=[env_id],
            user_ids=[user.id],
        )


# ==============================================================================
# Task 4.2: Concurrent release calls with overlapping reads (atomic claim)
# ==============================================================================


async def test_overlapping_read_release_race(async_engine: AsyncEngine):
    """Two concurrent release calls both read the child booking in READY status before claiming.

    Atomic claim guarantees that only the winning transition emits teardown dispatch.
    Both calls return successfully without duplicate side effects.
    """
    user = await _seed_user(async_engine)
    vm_id = uuid4()
    now = datetime.now(UTC)

    async with AsyncSession(async_engine, expire_on_commit=False) as session:
        session.add(
            BookingModel(
                id=vm_id,
                user_id=str(user.id),
                status=BookingStatus.READY.value,
                resource_type=ResourceType.VM.value,
                ttl_minutes=60,
                expires_at=now + timedelta(hours=1),
                created_at=now,
                cpus=2,
                memory_mb=2048,
                disk_mb=10240,
            )
        )
        await session.commit()

    created_bookings = [vm_id]

    try:
        dispatcher = MagicMock(spec=TaskDispatcher)
        booking_repo = BookingRepository()

        s1_read = asyncio.Event()
        s2_read = asyncio.Event()

        orig_get = booking_repo.get
        calls_s1 = 0
        calls_s2 = 0

        async def get_wrapper_s1(sess, b_id):
            nonlocal calls_s1
            calls_s1 += 1
            b = await orig_get(sess, b_id)
            if calls_s1 == 1:
                assert b.status == BookingStatus.READY
                s1_read.set()
                await asyncio.wait_for(s2_read.wait(), timeout=_TIMEOUT)
            return b

        async def get_wrapper_s2(sess, b_id):
            nonlocal calls_s2
            calls_s2 += 1
            b = await orig_get(sess, b_id)
            if calls_s2 == 1:
                assert b.status == BookingStatus.READY
                s2_read.set()
                await asyncio.wait_for(s1_read.wait(), timeout=_TIMEOUT)
                # Give session 1 a moment to acquire the lock and commit first
                await asyncio.sleep(0.05)
            return b

        async def run_release_s1():
            async with AsyncSession(async_engine, expire_on_commit=False) as session1:
                pid1 = (
                    await session1.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
                repo1 = BookingRepository()
                repo1.get = get_wrapper_s1
                uc1 = ReleaseBookingUseCase(repo1, dispatcher)
                res = await uc1.execute(session1, vm_id, user)
                return pid1, res

        async def run_release_s2():
            async with AsyncSession(async_engine, expire_on_commit=False) as session2:
                pid2 = (
                    await session2.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
                repo2 = BookingRepository()
                repo2.get = get_wrapper_s2
                uc2 = ReleaseBookingUseCase(repo2, dispatcher)
                res = await uc2.execute(session2, vm_id, user)
                return pid2, res

        results = await asyncio.gather(
            run_release_s1(), run_release_s2(), return_exceptions=True
        )
        unexpected = [r for r in results if isinstance(r, Exception)]
        assert unexpected == [], (
            f"Unexpected exceptions during overlapping read release: {unexpected}"
        )

        (pid1, b1), (pid2, b2) = results
        assert pid1 != pid2

        # Exactly ONE teardown dispatched
        dispatcher.dispatch_teardown.assert_called_once_with(
            str(vm_id), request_id=None
        )

        # Both returned RELEASING booking
        assert b1.status == BookingStatus.RELEASING
        assert b2.status == BookingStatus.RELEASING
    finally:
        await _cleanup(
            async_engine,
            booking_ids=created_bookings,
            user_ids=[user.id],
        )


# ==============================================================================
# Task 5.1: Pooled resource allocation racing with release
# ==============================================================================


@pytest.mark.parametrize(
    "resource_type", [ResourceType.NAMESPACE, ResourceType.STATIC_VM]
)
async def test_allocation_races_with_release(
    async_engine: AsyncEngine, resource_type: ResourceType
):
    """Pooled resource allocation races with the release of the sole held resource.

    Verifies serialization: no lost updates or duplicate ownership occurs.
    """
    user = await _seed_user(async_engine)
    created_bookings: list[UUID] = []

    if resource_type == ResourceType.NAMESPACE:
        res_id = await _seed_namespace(async_engine)
        ns_ids = [res_id]
        svm_ids = []
        fk_field = "namespace_id"
    else:
        res_id = await _seed_static_vm(async_engine)
        ns_ids = []
        svm_ids = [res_id]
        fk_field = "static_vm_id"

    # Seed initial booking holding the resource
    now = datetime.now(UTC)
    held_booking_id = uuid4()
    async with AsyncSession(async_engine, expire_on_commit=False) as session:
        session.add(
            BookingModel(
                id=held_booking_id,
                user_id=str(user.id),
                status=BookingStatus.READY.value,
                resource_type=resource_type.value,
                ttl_minutes=60,
                expires_at=now + timedelta(hours=1),
                created_at=now,
                cpus=0,
                memory_mb=0,
                disk_mb=0,
                **{fk_field: res_id},
            )
        )
        await session.commit()
    created_bookings.append(held_booking_id)

    try:
        async with _isolate_scarce_resource(async_engine, resource_type, res_id):
            dispatcher = MagicMock(spec=TaskDispatcher)
            booking_repo = BookingRepository()
            release_uc = ReleaseBookingUseCase(booking_repo, dispatcher)

            pool_repo = (
                NamespaceRepository()
                if resource_type == ResourceType.NAMESPACE
                else StaticVMRepository()
            )
            if resource_type == ResourceType.NAMESPACE:
                reserve_uc = BookNamespaceUseCase(booking_repo, pool_repo)
            else:
                reserve_uc = ReserveStaticVMUseCase(booking_repo, pool_repo)

            rel_in_flight = asyncio.Event()
            alloc_evaluating = asyncio.Event()
            rel_committed = False

            orig_lock_next = pool_repo.lock_next_available

            async def hooked_lock_next(sess):
                await asyncio.wait_for(rel_in_flight.wait(), timeout=_TIMEOUT)
                assert not rel_committed, (
                    "Release transaction already committed before allocation queried"
                )
                try:
                    return await orig_lock_next(sess)
                finally:
                    alloc_evaluating.set()

            pool_repo.lock_next_available = hooked_lock_next

            async def run_release():
                async with AsyncSession(
                    async_engine, expire_on_commit=False
                ) as session_rel:
                    pid_rel = (
                        await session_rel.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    orig_commit = session_rel.commit

                    async def hooked_commit():
                        rel_in_flight.set()
                        await asyncio.wait_for(
                            alloc_evaluating.wait(), timeout=_TIMEOUT
                        )
                        await orig_commit()
                        nonlocal rel_committed
                        rel_committed = True

                    session_rel.commit = hooked_commit
                    try:
                        res = await release_uc.execute(
                            session_rel, held_booking_id, user
                        )
                        return pid_rel, res
                    finally:
                        rel_in_flight.set()

            async def run_allocation():
                async with AsyncSession(
                    async_engine, expire_on_commit=False
                ) as session_alloc:
                    pid_alloc = (
                        await session_alloc.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                    try:
                        res = await reserve_uc.execute(
                            session_alloc, ttl_minutes=60, user_id=str(user.id)
                        )
                        return pid_alloc, res
                    finally:
                        alloc_evaluating.set()

            results = await asyncio.gather(
                run_release(), run_allocation(), return_exceptions=True
            )
            unexpected = [r for r in results if isinstance(r, Exception)]
            assert unexpected == [], (
                f"Unexpected error in allocation/release race: {unexpected}"
            )

            (pid_rel, rel_b), (pid_alloc, alloc_b) = results
            assert pid_rel != pid_alloc
            created_bookings.append(alloc_b.id)

            assert rel_b.status == BookingStatus.RELEASED
            assert alloc_b.status in (BookingStatus.READY, BookingStatus.QUEUED)

            # Invariant: at no point can more than one active booking hold this resource
            async with AsyncSession(async_engine, expire_on_commit=False) as check_sess:
                stmt = select(BookingModel).where(
                    BookingModel.status.in_(["READY", "PROVISIONING", "CONFIGURING"]),
                    getattr(BookingModel, fk_field) == res_id,
                )
                active_holders = (await check_sess.execute(stmt)).scalars().all()
                assert len(active_holders) <= 1, (
                    f"Resource {res_id} has multiple active holders: {active_holders}"
                )
    finally:
        await _cleanup(
            async_engine,
            booking_ids=created_bookings,
            namespace_ids=ns_ids,
            static_vm_ids=svm_ids,
            user_ids=[user.id],
        )
