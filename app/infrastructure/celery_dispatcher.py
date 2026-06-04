"""Infrastructure adapter implementing the application's TaskDispatcher port over Celery.

Concrete tasks are imported lazily inside each method to avoid import cycles at module load
(the tasks import repositories which import models, etc.).
"""


class CeleryTaskDispatcher:
    def dispatch_provision(self, booking_id: str, image_id: str, hw_config_id: str) -> None:
        from app.tasks.provision import provision_vm_task
        provision_vm_task.delay(booking_id, image_id, hw_config_id)

    def dispatch_teardown(self, booking_id: str) -> None:
        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.delay(booking_id)
