from typing import Protocol


class TaskDispatcher(Protocol):
    """Port for dispatching background jobs, so the application layer never imports the
    concrete Celery tasks (preserves the one-way dependency rule)."""

    def dispatch_provision(self, booking_id: str, image_id: str, hw_config_id: str) -> None: ...

    def dispatch_teardown(self, booking_id: str) -> None: ...
