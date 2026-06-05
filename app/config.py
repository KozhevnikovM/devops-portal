from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://portal:portal@localhost:5432/portal"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://portal:portal@localhost:5432/portal"
    REDIS_URL: str = "redis://localhost:6379/0"
    USE_STUB_TERRAFORM: bool = True
    DEV_USER_ID: str = "dev-user-00000000"

    # URL prefix the app is mounted under when behind a reverse proxy on a subpath
    # (e.g. "/dp" for https://host/dp/). Empty = served at the root. Passed to FastAPI as
    # root_path so the generated docs/OpenAPI URLs carry the prefix.
    ROOT_PATH: str = ""

    # Auth
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"
    SESSION_TTL: int = 86400
    # Send the session cookie only over HTTPS. Default True (production runs behind TLS);
    # set False for local development over plain http://localhost.
    SESSION_COOKIE_SECURE: bool = True

    # Per-user resource quotas (defaults applied when no per-user row exists)
    DEFAULT_QUOTA_CPUS: int = 16
    DEFAULT_QUOTA_MEMORY_GB: int = 32
    DEFAULT_QUOTA_SSD_GB: int = 500
    DEFAULT_QUOTA_HDD_GB: int = 500

    # Celery provision task
    PROVISION_MAX_RETRIES: int = 3
    PROVISION_RETRY_DELAY: int = 120   # seconds
    PROVISION_RATE_LIMIT: str = "0.5/m"

    # Celery beat tasks
    ENFORCE_TTL_INTERVAL_SECONDS: int = 60       # how often to release expired bookings
    STALE_PROVISIONING_THRESHOLD_MINUTES: int = 60

    # Terraform / VCD — only required when USE_STUB_TERRAFORM=False
    TF_WORKSPACES_DIR: str = "/tmp/tf-workspaces"
    TF_PG_CONN_STR: str = "postgresql://portal:portal@postgres:5432/portal?sslmode=disable"
    TF_MODULE_SOURCE: str = "/app/terraform/modules/vapp_vm"
    TF_APPLY_REFRESH: bool = True
    TF_APPLY_PARALLELISM: int = 1
    VCD_URL: str = ""
    VCD_NETWORK_NAME: str = ""
    VCD_ORG: str = ""
    VCD_VDC: str = ""
    VCD_API_TOKEN: str = ""
    VCD_API_TOKENS: str = ""   # comma-separated; overrides VCD_API_TOKEN when set
    VCD_TOKEN_LOCK_TTL: int = 900   # Redis lock TTL in seconds
    VCD_TOKEN_MAX_PARALLEL: int = 4   # max concurrent provisioning jobs per token
    VCD_USER: str = ""
    VCD_PASSWORD: str = ""
    VCD_ALLOW_UNVERIFIED_SSL: bool = False


settings = Settings()
