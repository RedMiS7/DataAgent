from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

# Keys saved from the web page, mapped to the Settings fields they fill.
SAVED_KEY_FIELDS = {"LLM_API_KEY": "llm_api_key", "EMBEDDING_API_KEY": "embedding_api_key"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    app_port: int = 8000
    local_env_file: Path = PROJECT_DIR / ".env.local"
    as_of_date: date = date(2026, 10, 31)
    log_level: str = "INFO"

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_business_db: str = "safe"
    mysql_app_db: str = "dataagent"
    mysql_ro_user: str = "shop_ro"
    mysql_ro_password: str = "dataagent_ro_pwd"
    mysql_admin_user: str = "root"
    mysql_admin_password: str = "change_me_root"

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection_prefix: str = "dataagent"

    llm_provider: str = "deepseek"
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 90.0

    embedding_provider: str = "qwen"
    embedding_endpoint: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_dim: int = 1024
    embedding_batch_size: int = 10

    retrieval_top_k: int = 6
    retrieval_min_score: float = 0.35

    sql_max_rows: int = 500
    sql_timeout_ms: int = 15000
    sql_max_retry: int = 3
    empty_max_retry: int = 1

    def _url(self, user: str, password: str, database: str) -> URL:
        return URL.create(
            "mysql+pymysql",
            username=user,
            password=password,
            host=self.mysql_host,
            port=self.mysql_port,
            database=database,
            query={"charset": "utf8mb4"},
        )

    @property
    def business_db_url(self) -> URL:
        return self._url(self.mysql_ro_user, self.mysql_ro_password, self.mysql_business_db)

    @property
    def app_db_url(self) -> URL:
        return self._url(self.mysql_admin_user, self.mysql_admin_password, self.mysql_app_db)

    @property
    def admin_server_url(self) -> URL:
        return URL.create(
            "mysql+pymysql",
            username=self.mysql_admin_user,
            password=self.mysql_admin_password,
            host=self.mysql_host,
            port=self.mysql_port,
            query={"charset": "utf8mb4"},
        )

    @property
    def data_dir(self) -> Path:
        return PROJECT_DIR / "data"

    @property
    def seed_dir(self) -> Path:
        return self.data_dir / "seed"

    def collection_name(self, suffix: str) -> str:
        return f"{self.qdrant_collection_prefix}_{suffix}"


def read_saved_keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    saved: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name in SAVED_KEY_FIELDS and value:
            saved[name] = value
    return saved


def apply_saved_keys(settings: Settings) -> None:
    """Keys saved from the web page take priority over `.env`."""
    for name, value in read_saved_keys(settings.local_env_file).items():
        setattr(settings, SAVED_KEY_FIELDS[name], value)


def save_keys(settings: Settings, keys: dict[str, str]) -> None:
    """Persist keys readable only by the service, then apply them to `settings`."""
    path = settings.local_env_file
    saved = read_saved_keys(path) | keys
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.writelines(f"{name}={value}\n" for name, value in saved.items())
    temporary.replace(path)
    apply_saved_keys(settings)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    apply_saved_keys(settings)
    return settings