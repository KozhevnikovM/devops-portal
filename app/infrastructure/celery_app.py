from celery import Celery

from app.config import settings

celery_app = Celery(
    "devops_portal",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.provision", "app.tasks.teardown", "app.tasks.beat_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "enforce-ttl": {
            "task":     "app.tasks.beat_tasks.enforce_ttl",
            "schedule": 300,   # every 5 min
        },
        "reap-stale-provisioning": {
            "task":     "app.tasks.beat_tasks.reap_stale_provisioning",
            "schedule": 900,   # every 15 min
        },
    },
)
