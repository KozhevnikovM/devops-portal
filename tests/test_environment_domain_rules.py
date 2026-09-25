"""#434 — domain rules: environment-child release error, derived environment status, lease start."""
import pytest

from app.domain.booking_status import CAN_BECOME_READY
from app.domain.enums import BookingStatus as S
from app.domain.environment_status import derive_environment_status
from app.domain.exceptions import BookingError, EnvironmentChildReleaseError
from app.domain.lease import lease_can_start


def test_environment_child_release_error_is_a_booking_error():
    # Routes map every BookingError to 409 — the new error must ride that mapping.
    assert issubclass(EnvironmentChildReleaseError, BookingError)


def test_can_become_ready_is_the_in_flight_set():
    assert CAN_BECOME_READY == {S.QUEUED, S.PENDING, S.PROVISIONING, S.CONFIGURING, S.RETRY}
    assert S.RELEASING not in CAN_BECOME_READY


@pytest.mark.parametrize("statuses, expected", [
    ([], S.READY),
    ([S.READY, S.FAILED], S.FAILED),
    ([S.RELEASED, S.FAILED], S.FAILED),
    ([S.READY, S.PROVISIONING], S.PROVISIONING),
    ([S.READY, S.QUEUED], S.PROVISIONING),
    ([S.RELEASED, S.RELEASED], S.RELEASED),
    ([S.READY, S.READY], S.READY),
    ([S.RELEASED, S.READY], S.FAILED),     # the #434 orphan — never READY
    ([S.RELEASING, S.READY], S.FAILED),
    ([S.RELEASING, S.RELEASED], S.FAILED),
])
def test_derive_environment_status(statuses, expected):
    assert derive_environment_status(statuses) is expected


@pytest.mark.parametrize("statuses, expected", [
    ([S.READY, S.READY], True),
    ([S.READY, S.FAILED], True),
    ([S.READY, S.RELEASED], True),
    ([S.READY, S.RELEASING], True),
    ([S.READY, S.QUEUED], False),
    ([S.READY, S.PENDING], False),
    ([S.READY, S.PROVISIONING], False),
    ([S.READY, S.CONFIGURING], False),
    ([S.READY, S.RETRY], False),
    ([S.PROVISIONING, S.RELEASING], False),
    ([S.RELEASING], False),
    ([S.RELEASING, S.FAILED], False),
    ([S.FAILED, S.FAILED], False),
    ([], False),
])
def test_lease_can_start(statuses, expected):
    assert lease_can_start(statuses) is expected
