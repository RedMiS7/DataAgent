from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    app_port: int = 8000
    as_of_date: date = date(2026, 10, 31)

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_business_db: str = "safe"
    mysql_app_db: str = "dataagent"
    mysql_ro_user: str = "shop_ro"
    mysql_ro_password: str = ""
    mysql_admin_user: str = "root"
    mysql_admin_password: str = ""

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


@lru_cache
def get_settings() -> Settings:
    return Settings()