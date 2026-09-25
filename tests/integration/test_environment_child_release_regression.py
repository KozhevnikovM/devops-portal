"""Integration: the #434 end-to-end regression scenario, on real Postgres with the stub adapter.

1. Order an environment with a namespace and a provisioned VM.
2. Try to release the namespace child on its own before the environment is READY → rejected (409).
3. Let the VM reach READY (the real provision task, stub Terraform).
4. The environment must not be left with a live VM on the far-future placeholder expiry: its lease
   has started and its derived status is READY.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.environment_status import derive_environment_status
from app.domain.exceptions import EnvironmentChildReleaseError
from app.infrastructure.database.models import (
    EnvironmentBlueprintItemModel,
    EnvironmentBlueprintModel,
    HWConfigModel,
    NamespaceModel,
    QuotaModel,
    VMImageModel,
)
from app.presentation import deps
from tests.integration._environment_lease import cleanup, make_sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_releasing_a_namespace_child_cannot_orphan_the_vm(async_engine: AsyncEngine, seed_user):
    tag = uuid4().hex[:8]
    image_id, hw_id, ns_id, bp_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        s.add(VMImageModel(id=image_id, name=f"img-434-{tag}", vapp_template_id="tpl", is_active=True))
        s.add(HWConfigModel(id=hw_id, name=f"hw-434-{tag}", cpus=1, memory_mb=1024, disk_mb=10240,
                            drive_type="HDD", is_active=True))
        s.add(NamespaceModel(id=ns_id, name=f"ns-434-{tag}", cluster_name=f"cl-434-{tag}",
                             api_url=None, is_active=True))
        bp = EnvironmentBlueprintModel(id=bp_id, name=f"bp-434-{tag}", description=None, is_active=True)
        bp.items = [
            EnvironmentBlueprintItemModel(resource_type="NAMESPACE", position=0, label="ns",
                                          spec={"namespace_name": f"ns-434-{tag}",
                                                "cluster_name": f"cl-434-{tag}"}),
            EnvironmentBlueprintItemModel(resource_type="VM", position=1, label="web",
                                          spec={"image_name": f"img-434-{tag}",
                                                "hw_config_name": f"hw-434-{tag}"}),
        ]
        s.add(bp)
        await s.commit()

    owner = User(id=seed_user, username="owner", password_hash="", role="user", is_active=True,
                 created_at=None)
    Session = make_sessionmaker()
    env_id = None
    dispatcher = MagicMock()
    try:
        # 1. Order (provisioning is dispatched to a recorder; step 3 runs it for real).
        with patch.object(deps.order_environment_uc, "_dispatcher", dispatcher):
            async with AsyncSession(async_engine, expire_on_commit=False) as s:
                env = await deps.order_environment_uc.execute(
                    s, blueprint_name=f"bp-434-{tag}", ttl_minutes=60, user_id=str(seed_user),
                )
        env_id = env.id
        ns_child = next(b for b in env.bookings if b.resource_type == ResourceType.NAMESPACE)
        vm_child = next(b for b in env.bookings if b.resource_type == ResourceType.VM)
        assert ns_child.status == BookingStatus.READY and vm_child.status == BookingStatus.PENDING
        assert env.expires_at == PERMANENT_EXPIRES_AT  # lease pending until the stack settles

        # 2. Release the namespace child on its own → rejected, nothing changes.
        with patch.object(deps.release_booking_uc, "_dispatcher", MagicMock()):
            async with AsyncSession(async_engine, expire_on_commit=False) as s:
                with pytest.raises(EnvironmentChildReleaseError, match=str(env_id)):
                    await deps.release_booking_uc.execute(s, ns_child.id, owner)

        # 3. The VM reaches READY through the real provision task (stub Terraform).
        with patch("app.tasks.provision.SyncSessionLocal", Session), \
             patch("app.tasks.provision.asyncio.run", return_value={"ip": "192.168.100.9"}):
            from app.tasks.provision import provision_vm_task
            provision_vm_task.run(str(vm_child.id), str(image_id), str(hw_id))

        # 4. Not orphaned: the lease started and the stack is a healthy READY.
        async with AsyncSession(async_engine, expire_on_commit=False) as s:
            env = await deps.env_repo.get(s, env_id)
        assert [b.status for b in env.bookings] == [BookingStatus.READY, BookingStatus.READY]
        assert env.expires_at != PERMANENT_EXPIRES_AT
        assert all(b.expires_at == env.expires_at for b in env.bookings)
        assert derive_environment_status(b.status for b in env.bookings) == BookingStatus.READY
    finally:
        if env_id is not None:
            cleanup(Session, [env_id])
        async with AsyncSession(async_engine) as s:
            await s.execute(delete(EnvironmentBlueprintModel).where(EnvironmentBlueprintModel.id == bp_id))
            await s.execute(delete(NamespaceModel).where(NamespaceModel.id == ns_id))
            await s.execute(delete(VMImageModel).where(VMImageModel.id == image_id))
            await s.execute(delete(HWConfigModel).where(HWConfigModel.id == hw_id))
            # Ordering auto-creates the owner's default quota row; drop it so seed_user can be removed.
            await s.execute(delete(QuotaModel).where(QuotaModel.user_id == seed_user))
            await s.commit()
