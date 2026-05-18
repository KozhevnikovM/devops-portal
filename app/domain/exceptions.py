class BookingError(Exception):
    pass


class BookingNotFoundError(BookingError):
    pass


class AuthenticationError(Exception):
    pass


class PermissionError(Exception):
    pass
