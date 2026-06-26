class BookingError(Exception):
    pass


class BookingNotFoundError(BookingError):
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


class BlueprintNotFoundError(EnvironmentError):
    pass


class EnvironmentItemError(EnvironmentError):
    """A blueprint item references a catalog entry that doesn't exist / isn't active."""


class AuthenticationError(Exception):
    pass


class BookingPermissionError(Exception):
    pass


class NamespaceShareError(Exception):
    pass


class NamespaceShareSelfError(NamespaceShareError):
    pass


class NamespaceShareUserNotFoundError(NamespaceShareError):
    pass


class NamespaceShareDuplicateError(NamespaceShareError):
    pass


class NamespaceShareNotFoundError(NamespaceShareError):
    pass
