from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://unmix:unmix@postgres:5432/unmix"
    redis_url: str = "redis://redis:6379/0"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "unmix"
    minio_secret_key: str = "unmix-dev-secret"
    minio_secure: bool = False
    minio_bucket: str = "unmix"
    local_object_dir: str | None = None
    jwt_secret: str = "dev-change-me"
    tile_size: int = 1024
    max_tile_attempts: int = 4

    model_config = SettingsConfigDict(env_prefix="UNMIX_", env_file=".env")


@lru_cache
def get_settings() -> Settings:
    return Settings()
