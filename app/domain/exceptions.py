class NotFoundError(Exception):
    pass


class BookingError(Exception):
    pass


class BookingNotFoundError(NotFoundError, BookingError):
    pass


class QuotaExceededError(BookingError):
    pass


class NamespaceUnavailableError(BookingError):
    pass


class StaticVMUnavailableError(BookingError):
    pass


class IllegalStatusTransitionError(BookingError):
    """A booking status move that the transition machine forbids (#238)."""
    pass


class EnvironmentError(Exception):
    pass


class BlueprintNotFoundError(NotFoundError, EnvironmentError):
    pass


class EnvironmentItemError(EnvironmentError):
    """A blueprint item references a catalog entry that doesn't exist / isn't active."""


class EnvironmentNotFoundError(NotFoundError):
    pass


class NamespaceNotFoundError(NotFoundError):
    pass


class StaticVMNotFoundError(NotFoundError):
    pass


class ImageNotFoundError(NotFoundError):
    pass


class HWConfigNotFoundError(NotFoundError):
    pass


class RoleNotFoundError(NotFoundError):
    pass


class AuthenticationError(Exception):
    pass


class BookingPermissionError(Exception):
    pass


class SecretDecryptionError(BookingError):
    """Fernet key mismatch or corrupted ciphertext — permanent, must not be retried."""
