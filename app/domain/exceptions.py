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


class AuthenticationError(Exception):
    pass


class BookingPermissionError(Exception):
    pass
