from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://portal:portal@localhost:5432/portal"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://portal:portal@localhost:5432/portal"
    REDIS_URL: str = "redis://localhost:6379/0"
    USE_STUB_TERRAFORM: bool = True
    DEV_USER_ID: str = "dev-user-00000000"

    # Celery provision task
    PROVISION_MAX_RETRIES: int = 3
    PROVISION_RETRY_DELAY: int = 120   # seconds
    PROVISION_RATE_LIMIT: str = "0.5/m"

    # Celery beat tasks
    STALE_PROVISIONING_THRESHOLD_MINUTES: int = 60

    # Terraform / VCD — only required when USE_STUB_TERRAFORM=False
    TF_WORKSPACES_DIR: str = "/tmp/tf-workspaces"
    TF_PG_CONN_STR: str = "postgresql://portal:portal@postgres:5432/portal?sslmode=disable"
    TF_MODULE_SOURCE: str = "/app/terraform/modules/vapp_vm"
    TF_APPLY_REFRESH: bool = False
    TF_APPLY_PARALLELISM: int = 1
    VCD_URL: str = ""
    VCD_NETWORK_NAME: str = ""
    VCD_ORG: str = ""
    VCD_VDC: str = ""
    VCD_API_TOKEN: str = ""
    VCD_API_TOKENS: str = ""   # comma-separated; overrides VCD_API_TOKEN when set
    VCD_TOKEN_LOCK_TTL: int = 900   # Redis lock TTL in seconds
    VCD_TOKEN_MAX_PARALLEL: int = 1   # max concurrent provisioning jobs per token
    VCD_USER: str = ""
    VCD_PASSWORD: str = ""
    VCD_ALLOW_UNVERIFIED_SSL: bool = False


settings = Settings()
