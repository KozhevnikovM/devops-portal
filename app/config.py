from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str = "postgresql+asyncpg://portal:portal@localhost:5432/portal"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://portal:portal@localhost:5432/portal"
    REDIS_URL: str = "redis://localhost:6379/0"
    USE_STUB_TERRAFORM: bool = True
    DEV_USER_ID: str = "dev-user-00000000"


settings = Settings()
