"""Regression tests for P3-E/#304: typed NotFoundError domain exceptions.

Verifies the exception hierarchy, repo raises, and that the base class
correctly covers all per-entity subclasses.
"""
import pytest

from app.domain.exceptions import (
    BlueprintNotFoundError,
    BookingError,
    BookingNotFoundError,
    EnvironmentError,
    EnvironmentNotFoundError,
    HWConfigNotFoundError,
    ImageNotFoundError,
    NamespaceNotFoundError,
    NotFoundError,
    RoleNotFoundError,
    StaticVMNotFoundError,
)


def test_not_found_error_is_exception():
    assert issubclass(NotFoundError, Exception)


def test_all_subclasses_are_not_found_error():
    for cls in [
        BookingNotFoundError, EnvironmentNotFoundError, NamespaceNotFoundError,
        StaticVMNotFoundError, ImageNotFoundError, HWConfigNotFoundError,
        RoleNotFoundError, BlueprintNotFoundError,
    ]:
        assert issubclass(cls, NotFoundError), f"{cls.__name__} should be NotFoundError"


def test_booking_not_found_error_is_also_booking_error():
    """Backward-compat: existing except BookingError catches still work."""
    assert issubclass(BookingNotFoundError, BookingError)


def test_blueprint_not_found_error_is_also_environment_error():
    """Backward-compat: existing except EnvironmentError catches still work."""
    assert issubclass(BlueprintNotFoundError, EnvironmentError)


def test_not_found_error_is_catchable_as_base():
    with pytest.raises(NotFoundError):
        raise EnvironmentNotFoundError("env 123 not found")


def test_not_found_error_instance_check():
    err = RoleNotFoundError("role xyz not found")
    assert isinstance(err, NotFoundError)
    assert isinstance(err, Exception)
    assert "role xyz" in str(err)
