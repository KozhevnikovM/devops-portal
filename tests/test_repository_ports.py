"""Conformance: the concrete repositories structurally satisfy their application-layer ports (#239).

`@runtime_checkable` Protocols let `isinstance` verify the method *names* are present, so a future
signature/rename drift between a concrete repo and its port fails fast here.
"""

import pytest

from app.application import ports
from app.infrastructure.config.ansible import AnsibleConfigRunner, StubAnsibleRunner
from app.infrastructure.config.runner import SshConfigRunner, StubConfigRunner
from app.infrastructure.progress_recorder import ProgressRecorder
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_blueprint_repo import (
    EnvironmentBlueprintRepository,
)
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.quota_repo import QuotaRepository
from app.infrastructure.repositories.role_repo import RoleRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository


@pytest.mark.parametrize(
    "repo, port",
    [
        (BookingRepository(), ports.BookingRepositoryPort),
        (ImageRepository(), ports.ImageRepositoryPort),
        (HWConfigRepository(), ports.HWConfigRepositoryPort),
        (QuotaRepository(), ports.QuotaRepositoryPort),
        (NamespaceRepository(), ports.PooledResourceRepositoryPort),
        (NamespaceRepository(), ports.NamespaceRepositoryPort),
        (StaticVMRepository(), ports.PooledResourceRepositoryPort),
        (StaticVMRepository(), ports.StaticVMRepositoryPort),
        (EnvironmentRepository(), ports.EnvironmentRepositoryPort),
        (EnvironmentBlueprintRepository(), ports.BlueprintRepositoryPort),
        (RoleRepository(), ports.RoleRepositoryPort),
        (BookingRepository(), ports.SyncBookingRepositoryPort),
        (ImageRepository(), ports.SyncImageReadPort),
        (HWConfigRepository(), ports.SyncHWConfigReadPort),
        (EnvironmentRepository(), ports.SyncEnvironmentLeasePort),
        (SshConfigRunner(), ports.VmConfigRunnerPort),
        (StubConfigRunner(), ports.VmConfigRunnerPort),
        (AnsibleConfigRunner(), ports.RoleApplierPort),
        (StubAnsibleRunner(), ports.RoleApplierPort),
        (
            ProgressRecorder(
                lambda chunk, last, acc: None,
                interval_s=1.0,
                message_threshold=1,
                char_threshold=1,
            ),
            ports.ProgressSink,
        ),
    ],
)
def test_repo_satisfies_port(repo, port):
    assert isinstance(repo, port)


def test_unrelated_object_does_not_satisfy_port():
    assert not isinstance(object(), ports.BookingRepositoryPort)
