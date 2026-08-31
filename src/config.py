from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    database_url: str = "postgresql://user:password@localhost:5432/renthub"
    request_delay: float = 2.0
    max_concurrent_requests: int = 2
    max_pages: Optional[int] = None
    download_images: bool = False
    removed_after_missing_scans: int = 3
    detail_refresh_days: int = 7
    tz: str = "Asia/Bangkok"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    site_username: str = "admin"
    site_password: str = "changeme"
    secret_key: str = "change-this-to-a-random-secret"
    anthropic_api_key: Optional[str] = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
