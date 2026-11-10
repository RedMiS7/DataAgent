from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from .datasets import DatasetManifest, dataset_manifests

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
    as_of_date_override: date | None = Field(default=None, validation_alias="as_of_date")
    log_level: str = "INFO"

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_business_db: str = "safe"
    # The SQL dialect of the business database, as sqlglot names it.
    sql_dialect: str = "mysql"
    mysql_app_db: str = "dataagent"
    mysql_ro_user: str = "shop_ro"
    mysql_ro_password: str = "dataagent_ro_pwd"
    # The service reads and writes the app database with this account, and fills the
    # empty shop database with demo orders. It holds no other rights.
    mysql_app_user: str = "dataagent_app"
    mysql_app_password: str = "dataagent_app_pwd"
    # Evaluation runs the reference SQL and reads the logs with this account: read-only
    # on the views and the app database. No default password: grant_eval_user creates
    # the account with the one set in .env.
    mysql_eval_user: str = "dataagent_eval"
    mysql_eval_password: str = ""
    # Only for admin commands: import_dataset, grant_app_user and grant_eval_user. The
    # service never connects with it.
    mysql_admin_user: str = "root"
    mysql_admin_password: str = "change_me_root"

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection_prefix: str = "dataagent"

    llm_provider: str = "deepseek"
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_temperature: float = 0.0
    # One model call; a whole question has its own limit below.
    llm_timeout_seconds: float = 90.0
    # Output tokens of one model call. At 1024 a long analysis SQL was cut off
    # mid-statement; deepseek-chat allows up to 8192.
    llm_max_tokens: int = 8192
    # A whole question, from the first model call to the answer. Past it, the
    # queries done so far are shown as a partial answer.
    ask_timeout_seconds: float = 120.0
    # Analysis took up to 81 seconds in the 2026-11-07 evaluation.
    analysis_timeout_seconds: float = 180.0

    embedding_provider: str = "qwen"
    embedding_endpoint: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_dim: int = 1024
    embedding_batch_size: int = 10

    # Tokens for the table columns in the prompt. Under it every table goes in;
    # over it, only the tables a question needs. About half of the model's context:
    # deepseek-chat has 64K.
    schema_token_budget: int = 32000
    # How the model gets the tables. "agentic": a table catalog, the tables of the recent
    # SQL, and search_semantic for the rest. "full": every table in the prompt, as far as
    # the budget allows.
    schema_mode: str = "agentic"
    # Folder under data/ holding the semantic layer of the business database.
    semantic_seed: str = "seed"
    # Fill an empty shop database with demo orders at start-up. Off for other datasets,
    # which have no shop.orders table.
    generate_demo_data: bool = True

    # Rerank model for table retrieval. Empty or "off" turns it off; the key falls back
    # to the embedding key, as both are Bailian keys.
    rerank_provider: str = "qwen"
    rerank_endpoint: str = ""
    rerank_model: str = ""
    rerank_api_key: str = ""
    rerank_timeout_seconds: float = 5.0
    # Tables that go to the rerank model, and how many of them a search keeps.
    rerank_candidates: int = 20
    search_top_tables: int = 5

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
        return self._url(self.mysql_app_user, self.mysql_app_password, self.mysql_app_db)

    def eval_business_url(self, database: str | None = None) -> URL:
        """The evaluation account on a view database, the instance's own by default."""
        return self._url(self.mysql_eval_user, self.mysql_eval_password,
                         database or self.mysql_business_db)

    def eval_app_url(self) -> URL:
        return self._url(self.mysql_eval_user, self.mysql_eval_password, self.mysql_app_db)

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
    def dataset(self) -> DatasetManifest | None:
        return dataset_manifests(self.data_dir).get(self.mysql_business_db)

    @field_validator("as_of_date_override", mode="before")
    @classmethod
    def _ignore_blank_date(cls, value: Any) -> Any:
        # Compose can explicitly clear a date from a shared env file.
        return None if value == "" else value

    @property
    def as_of_date(self) -> date:
        if self.as_of_date_override is not None:
            return self.as_of_date_override
        dataset = self.dataset
        return dataset.as_of_date if dataset and dataset.as_of_date else date.today()

    @property
    def seed_dir(self) -> Path:
        return self.data_dir / self.semantic_seed

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
