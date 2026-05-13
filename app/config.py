from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://portal:portal@localhost:5432/portal"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://portal:portal@localhost:5432/portal"
    REDIS_URL: str = "redis://localhost:6379/0"
    USE_STUB_TERRAFORM: bool = True
    DEV_USER_ID: str = "dev-user-00000000"

    # Terraform / VCD — only required when USE_STUB_TERRAFORM=False
    TF_WORKSPACES_DIR: str = "/tmp/tf-workspaces"
    TF_MODULE_SOURCE: str = "/app/terraform/modules/vapp_vm"
    VCD_VAPP_NAME: str = ""
    VCD_NETWORK_NAME: str = ""
    VCD_VAPP_TEMPLATE_ID: str = ""
    VCD_ORG: str = ""
    VCD_VDC: str = ""


settings = Settings()
