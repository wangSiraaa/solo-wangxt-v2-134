from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UNMIX_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./unmix.db"
    # Object storage: "local" uses LOCAL_STORE_DIR; "minio" requires the minio.* values
    object_store: str = "local"
    local_store_dir: str = "./objectstore"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "unmix"

    # Task execution: empty/None -> eager in-process executor; redis://... -> Celery
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    tile_size: int = 512
    max_tile_attempts: int = 3
    matrix_cond_warn: float = 50.0
    saturation_fraction_warn: float = 0.01

    # Well-known seeded API keys (override in production!)
    seed_admin_key: str = "admin-key"
    seed_imager_key: str = "imager-key"
    seed_analyst_key: str = "analyst-key"
    seed_matrixer_key: str = "matrixer-key"
    seed_publisher_key: str = "publisher-key"


@lru_cache
def get_settings() -> Settings:
    return Settings()
