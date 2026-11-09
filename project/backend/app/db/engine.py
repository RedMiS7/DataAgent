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


@contextmanager
def eval_business_connection(settings: Settings,
                             database: str | None = None) -> Iterator[Connection]:
    """The evaluation account on a view database, with the same time limit as the agent."""
    url = settings.eval_business_url(database).render_as_string(hide_password=False)
    with _engine(url).connect() as conn:
        conn.exec_driver_sql(f"SET SESSION max_execution_time = {int(settings.sql_timeout_ms)}")
        yield conn


@contextmanager
def eval_app_connection(settings: Settings) -> Iterator[Connection]:
    """The evaluation account on the app database; it may only read."""
    with _engine(settings.eval_app_url().render_as_string(hide_password=False)).connect() as conn:
        yield conn


# MySQL refused the account: a wrong user or password (1045), or no rights on the
# database (1044). Waiting does not fix these.
ACCESS_DENIED_CODES = {1044, 1045}


class DatabaseAccessDenied(RuntimeError):
    pass


def is_access_denied(exc: Exception) -> bool:
    error = getattr(exc, "orig", None) or exc
    return bool(error.args) and error.args[0] in ACCESS_DENIED_CODES


def ping(engine: Engine, raise_denied: bool = False) -> bool:
    """Whether the database answers. With ``raise_denied``, a refused account raises
    DatabaseAccessDenied instead of returning False, so callers can stop waiting."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        if raise_denied and is_access_denied(exc):
            raise DatabaseAccessDenied(str(getattr(exc, "orig", None) or exc)) from exc
        return False