from celery import Celery
from celery.signals import setup_logging

from app.config import settings
from app.infrastructure.logging_config import configure_logging

celery_app = Celery(
    "devops_portal",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.provision", "app.tasks.teardown", "app.tasks.beat_tasks"],
)


@setup_logging.connect
def _configure_worker_logging(**kwargs) -> None:
    """Install the same root-logger config the `app` process uses (#371 F-1).

    Connecting to this signal — combined with `worker_hijack_root_logger=False` below — stops
    Celery from installing its own `[%(asctime)s: %(levelname)s/%(processName)s] %(message)s`
    root-logger config, so `worker`/`beat` end up with byte-for-byte the same JSON formatter as
    `app` instead of a differently-formatted (and differently-triggered) one.
    """
    configure_logging()


celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_hijack_root_logger=False,
    beat_schedule={
        "enforce-ttl": {
            "task":     "app.tasks.beat_tasks.enforce_ttl",
            "schedule": settings.ENFORCE_TTL_INTERVAL_SECONDS,
        },
        "enforce-environment-ttl": {
            "task":     "app.tasks.beat_tasks.enforce_environment_ttl",
            "schedule": settings.ENFORCE_TTL_INTERVAL_SECONDS,
        },
        "reap-stale-provisioning": {
            "task":     "app.tasks.beat_tasks.reap_stale_provisioning",
            "schedule": 900,   # every 15 min
        },
    },
)
