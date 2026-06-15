from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Environment
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BlueprintNotFoundError, EnvironmentItemError
from app.domain.lease import Lease


class OrderEnvironmentUseCase:
    """Order an environment blueprint: create a parent Environment + its child bookings.

    Blueprint item names are resolved up front (a bad name creates nothing). Children are created
    via the existing booking use cases, tagged with the environment id and the shared TTL; VM
    provisioning is dispatched only after every child is created, so a mid-order failure rolls the
    whole environment back (delete the created children + the environment) with nothing dispatched.
    """

    def __init__(
        self, env_repo, blueprint_repo, booking_repo, create_use_case,
        reserve_static_vm_use_case, book_namespace_use_case,
        image_repo, hw_config_repo, role_repo, static_vm_repo, dispatcher,
    ) -> None:
        self._env_repo = env_repo
        self._blueprint_repo = blueprint_repo
        self._booking_repo = booking_repo
        self._create = create_use_case
        self._reserve_static = reserve_static_vm_use_case
        self._book_namespace = book_namespace_use_case
        self._image_repo = image_repo
        self._hw_config_repo = hw_config_repo
        self._role_repo = role_repo
        self._static_vm_repo = static_vm_repo
        self._dispatcher = dispatcher

    async def execute(
        self, session: AsyncSession, blueprint_name: str, ttl_minutes: int, user_id: str,
        created_by: str | None = None,
    ) -> Environment:
        blueprint = await self._blueprint_repo.get_by_name(session, blueprint_name)
        if blueprint is None:
            raise BlueprintNotFoundError(f"No active blueprint named '{blueprint_name}'")

        # ── Resolve every item's names up front — a bad name creates nothing ──
        resolved = [await self._resolve_item(session, it) for it in blueprint.items]

        # The lease starts when the whole stack is READY (#223). Until then the environment's
        # expires_at is a far-future placeholder so a short TTL can't tear the stack down
        # mid-provision; it's stamped with the real deadline once every child is READY.
        env = await self._env_repo.create(
            session, name=blueprint.name, blueprint_name=blueprint.name,
            user_id=user_id, ttl_minutes=ttl_minutes, expires_at=Lease.pending(ttl_minutes).expires_at,
            created_by=created_by,
        )

        created_ids: list[UUID] = []
        vm_dispatch: list[tuple[str, str, str]] = []  # (booking_id, image_id, hw_config_id)
        try:
            for item, res in zip(blueprint.items, resolved):
                booking = await self._create_child(session, item, res, ttl_minutes, user_id, env.id, created_by)
                created_ids.append(booking.id)
                if item.resource_type == ResourceType.VM.value:
                    vm_dispatch.append((str(booking.id), str(res["image_id"]), str(res["hw_config_id"])))
        except Exception:
            await self._rollback(session, env.id, created_ids)
            raise

        # An all-pooled environment is fully READY at once — start its lease immediately. One with
        # VMs is stamped later, when the last child reaches READY in the provision task.
        await self._env_repo.start_lease_if_ready(session, env.id)

        # All children created — now dispatch VM provisioning (nothing dispatched before this point).
        for booking_id, image_id, hw_config_id in vm_dispatch:
            self._dispatcher.dispatch_provision(booking_id, image_id, hw_config_id)

        return await self._env_repo.get(session, env.id)

    async def _resolve_item(self, session, item) -> dict:
        spec = item.spec or {}
        rt = item.resource_type
        if rt == ResourceType.VM.value:
            image = await self._image_repo.get_by_name(session, spec.get("image_name"))
            if image is None:
                raise EnvironmentItemError(f"no VM image named '{spec.get('image_name')}'")
            hw = await self._hw_config_repo.get_by_name(session, spec.get("hw_config_name"))
            if hw is None:
                raise EnvironmentItemError(f"no hardware config named '{spec.get('hw_config_name')}'")
            config_roles = []
            for role_name in (spec.get("roles") or []):
                role = await self._role_repo.get_by_name(session, role_name)
                if role is None:
                    raise EnvironmentItemError(f"no role named '{role_name}'")
                config_roles.append(
                    {"name": role.name, "ansible_role": role.ansible_role, "vars": role.default_vars or {}}
                )
            return {"image_id": image.id, "hw_config_id": hw.id, "config_roles": config_roles,
                    "startup_script": spec.get("startup_script")}
        if rt == ResourceType.STATIC_VM.value:
            static_vm_id = None
            if spec.get("static_vm_name"):
                vm = await self._static_vm_repo.get_by_name(session, spec["static_vm_name"])
                if vm is None:
                    raise EnvironmentItemError(f"no static VM named '{spec['static_vm_name']}'")
                static_vm_id = vm.id
            return {"static_vm_id": static_vm_id}
        # NAMESPACE — resolved by the use case from name+cluster (or any-available)
        return {"namespace_name": spec.get("namespace_name"), "cluster_name": spec.get("cluster_name")}

    async def _create_child(self, session, item, res, ttl_minutes, user_id, env_id, created_by=None):
        rt = item.resource_type
        label = item.label
        if rt == ResourceType.VM.value:
            return await self._create.execute(
                session, ttl_minutes, res["image_id"], res["hw_config_id"], user_id=user_id,
                startup_script=res["startup_script"], config_roles=res["config_roles"],
                environment_id=env_id, environment_label=label, created_by=created_by, dispatch=False,
            )
        if rt == ResourceType.STATIC_VM.value:
            return await self._reserve_static.execute(
                session, ttl_minutes, user_id=user_id,
                static_vm_id=res["static_vm_id"], environment_id=env_id, environment_label=label,
                created_by=created_by,
            )
        return await self._book_namespace.execute(
            session, ttl_minutes, user_id=user_id,
            namespace_name=res["namespace_name"], cluster_name=res["cluster_name"],
            environment_id=env_id, environment_label=label, created_by=created_by,
        )

    async def _rollback(self, session, env_id, booking_ids) -> None:
        """Best-effort: release the children created in this order (terminal status frees any pooled
        resource; the VMs were never dispatched, so no teardown is needed), then delete the
        environment row. Leaves the whole order effectively undone."""
        for bid in booking_ids:
            try:
                await self._booking_repo.update_status(session, bid, BookingStatus.RELEASED)
            except Exception:
                pass
        try:
            await self._env_repo.delete(session, env_id)
        except Exception:
            pass
