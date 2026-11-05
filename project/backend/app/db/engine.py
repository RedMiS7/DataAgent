from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from ..config import Settings


@lru_cache(maxsize=8)
def _engine(dsn: str) -> Engine:
    return create_engine(dsn, pool_pre_ping=True, pool_recycle=3600, pool_size=5, max_overflow=5)


def business_engine(settings: Settings) -> Engine:
    return _engine(settings.business_db_url.render_as_string(hide_password=False))


def app_engine(settings: Settings) -> Engine:
    return _engine(settings.app_db_url.render_as_string(hide_password=False))


@contextmanager
def business_connection(settings: Settings) -> Iterator[Connection]:
    with business_engine(settings).connect() as conn:
        conn.exec_driver_sql(f"SET SESSION max_execution_time = {int(settings.sql_timeout_ms)}")
        yield conn


@contextmanager
def app_connection(settings: Settings) -> Iterator[Connection]:
    with app_engine(settings).begin() as conn:
        yield conn


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False