"""Composition root — the single place the object graph is assembled (#239 PR 3).

Concrete repositories, the Celery dispatcher, and the use cases are constructed **once** here and
shared by every route module (and importable by the Celery side). This is where the abstraction →
implementation wiring lives, so the application-layer use cases stay free of concrete
`app.infrastructure` imports (the `application → infrastructure` rule, see
`docs/refactor/repository-interfaces.md`).

Repositories are stateless (the session is passed per call), so sharing one instance across routes is
safe. Route modules bind their existing module-level names to these singletons, e.g.
``_repo = deps.booking_repo`` — keeping those names means tests that patch them by module path keep
working.
"""
from app.application.use_cases.book_namespace import BookNamespaceUseCase
from app.application.use_cases.create_booking import CreateBookingUseCase
from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.application.use_cases.order_environment import OrderEnvironmentUseCase
from app.application.use_cases.release_booking import ReleaseBookingUseCase
from app.application.use_cases.release_environment import ReleaseEnvironmentUseCase
from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase
from app.infrastructure.celery_dispatcher import CeleryTaskDispatcher
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_blueprint_repo import EnvironmentBlueprintRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.quota_repo import QuotaRepository
from app.infrastructure.repositories.role_repo import RoleRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

# ── Repositories (shared, stateless) ─────────────────────────────────────────────
booking_repo = BookingRepository()
image_repo = ImageRepository()
hw_config_repo = HWConfigRepository()
quota_repo = QuotaRepository()
namespace_repo = NamespaceRepository()
static_vm_repo = StaticVMRepository()
env_repo = EnvironmentRepository()
blueprint_repo = EnvironmentBlueprintRepository()
role_repo = RoleRepository()

# ── Dispatcher (Celery) ──────────────────────────────────────────────────────────
dispatcher = CeleryTaskDispatcher()

# ── Use cases ────────────────────────────────────────────────────────────────────
create_booking_uc = CreateBookingUseCase(
    booking_repo, image_repo, hw_config_repo, quota_repo=quota_repo, dispatcher=dispatcher,
)
extend_booking_uc = ExtendBookingUseCase(booking_repo)
release_booking_uc = ReleaseBookingUseCase(booking_repo, dispatcher)
book_namespace_uc = BookNamespaceUseCase(booking_repo, namespace_repo)
reserve_static_vm_uc = ReserveStaticVMUseCase(booking_repo, static_vm_repo)
order_environment_uc = OrderEnvironmentUseCase(
    env_repo, blueprint_repo, booking_repo, create_booking_uc, reserve_static_vm_uc,
    book_namespace_uc, image_repo, hw_config_repo, role_repo, static_vm_repo, dispatcher,
)
release_environment_uc = ReleaseEnvironmentUseCase(env_repo, release_booking_uc)
