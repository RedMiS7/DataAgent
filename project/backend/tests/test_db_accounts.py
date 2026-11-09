"""The service runs with its own MySQL account; root is only for admin commands."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pymysql
import pytest
from app import bootstrap, demo_data, importer
from app.config import PROJECT_DIR, Settings
from app.db import accounts
from app.db.engine import DatabaseAccessDenied, is_access_denied
from app.importer import Manifest
from app.semantic import cli
from sqlalchemy.exc import OperationalError


def make_settings(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(_env_file=None, local_env_file=tmp_path / ".env.local", **kwargs)


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, statement: Any, params: dict | None = None) -> None:
        self.calls.append((str(statement), params))

    @property
    def sql(self) -> list[str]:
        return [statement for statement, _ in self.calls]


class RecordingEngine:
    def __init__(self, conn: RecordingConnection) -> None:
        self.conn = conn
        self.disposed = False

    @contextmanager
    def begin(self):
        yield self.conn

    def dispose(self) -> None:
        self.disposed = True


def test_the_app_database_is_reached_with_the_app_account(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mysql_app_user="svc", mysql_app_password="pw",
                             mysql_admin_user="root", mysql_ro_user="reader")
    assert settings.app_db_url.username == "svc"
    assert settings.app_db_url.password == "pw"
    assert settings.app_db_url.database == settings.mysql_app_db
    assert settings.business_db_url.username == "reader"
    assert settings.admin_server_url.username == "root"


def test_the_default_app_account_is_not_root(tmp_path: Path) -> None:
    assert make_settings(tmp_path).app_db_url.username == "dataagent_app"


def test_grants_cover_the_app_database_and_optionally_the_demo_database() -> None:
    assert accounts.grant_statements("svc", "dataagent", None) == [
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON `dataagent`.* "
        "TO 'svc'@'%'"]
    with_demo = accounts.grant_statements("svc", "dataagent", "shop")
    assert with_demo[1] == "GRANT SELECT, INSERT ON `shop`.* TO 'svc'@'%'"
    # No right to drop tables or to pass rights on.
    assert not any(re.search(r"\b(DROP|GRANT OPTION|ALL)\b", s.split(" ON ")[0][6:])
                   for s in with_demo)


@pytest.mark.parametrize("user, database", [("svc'; DROP USER root; --", "dataagent"),
                                            ("svc", "dataagent`.*"), ("Svc", "dataagent")])
def test_names_that_are_not_plain_identifiers_are_rejected(user: str, database: str) -> None:
    with pytest.raises(ValueError, match="只能用小写字母"):
        accounts.grant_statements(user, database, None)


def test_the_password_is_a_bound_value_not_part_of_the_sql() -> None:
    conn = RecordingConnection()
    statements = accounts.ensure_app_user(conn, "svc", "p'w\"d", "dataagent", "shop")  # type: ignore[arg-type]
    assert conn.sql[0] == "CREATE USER IF NOT EXISTS 'svc'@'%' IDENTIFIED BY :password"
    assert conn.sql[1] == "ALTER USER 'svc'@'%' IDENTIFIED BY :password"
    assert conn.calls[0][1] == {"password": "p'w\"d"}
    assert not any("p'w" in sql for sql in conn.sql)
    assert conn.sql[2:] == statements


def test_an_empty_password_is_refused() -> None:
    with pytest.raises(ValueError, match="MYSQL_APP_PASSWORD"):
        accounts.ensure_app_user(RecordingConnection(), "svc", "", "dataagent")  # type: ignore[arg-type]


def test_the_init_script_grants_what_the_code_grants() -> None:
    script = (PROJECT_DIR / "data" / "initdb" / "03_grants.sql").read_text(encoding="utf-8")
    assert (f"GRANT {accounts.APP_DATABASE_RIGHTS} ON dataagent.* TO 'dataagent_app'@'%';"
            in script)
    assert (f"GRANT {accounts.DEMO_DATABASE_RIGHTS} ON {demo_data.DEMO_DATABASE}.* "
            "TO 'dataagent_app'@'%';" in script)
    assert "IDENTIFIED BY 'dataagent_app_pwd'" in script
    assert Settings(_env_file=None).mysql_app_password == "dataagent_app_pwd"


def test_demo_orders_are_written_with_the_app_account(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: seen.update(kwargs))
    demo_data._connect(make_settings(tmp_path, mysql_app_user="svc", mysql_app_password="pw"))
    assert (seen["user"], seen["password"], seen["database"]) == ("svc", "pw", "shop")


def test_a_refused_account_is_detected() -> None:
    refused = OperationalError("SELECT 1", {}, pymysql.err.OperationalError(1045, "Access denied"))
    down = OperationalError("SELECT 1", {}, pymysql.err.OperationalError(2003, "Can't connect"))
    assert is_access_denied(refused)
    assert not is_access_denied(down)


def test_setup_stops_at_once_and_names_the_command_when_the_account_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(engine: Any, raise_denied: bool = False) -> bool:
        assert raise_denied
        raise DatabaseAccessDenied("(1045, \"Access denied for user 'dataagent_app'\")")

    monkeypatch.setattr(bootstrap, "app_engine", lambda settings: None)
    monkeypatch.setattr(bootstrap, "ping", refused)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: pytest.fail("must not wait"))
    runner = bootstrap.Bootstrapper(make_settings(tmp_path), None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError) as error:
        runner._connect_database()
    assert "dataagent_app" in str(error.value)
    assert "grant_app_user" in str(error.value)


@pytest.mark.parametrize("demo, expected", [(True, 2), (False, 1)])
def test_grant_app_user_grants_this_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str],
                                             demo: bool, expected: int) -> None:
    conn = RecordingConnection()
    engine = RecordingEngine(conn)
    urls: list[str] = []

    def fake_engine(url: str) -> RecordingEngine:
        urls.append(url)
        return engine

    settings = make_settings(tmp_path, mysql_app_db="dataagent_dabench",
                             generate_demo_data=demo, mysql_admin_password="secret")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_engine", fake_engine)
    assert cli.main(["grant_app_user"]) == 0
    assert urls[0].startswith("mysql+pymysql://root:")
    grants = [sql for sql in conn.sql if sql.startswith("GRANT")]
    assert len(grants) == expected
    assert "`dataagent_dabench`" in grants[0]
    assert engine.disposed
    output = capsys.readouterr().out
    assert "dataagent_app" in output and "secret" not in output


def test_importing_a_dataset_grants_its_app_database(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(importer, "create_engine", lambda url: RecordingEngine(conn))
    manifest = Manifest(raw_database="ds_raw", database="ds", app_database="dataagent_ds",
                        data_dir=tmp_path, tables=[])
    importer.import_dataset(make_settings(tmp_path), manifest,
                            PROJECT_DIR / "data" / "initdb" / "02_app_schema.sql")
    grants = [sql for sql in conn.sql if sql.startswith("GRANT")]
    assert grants[0] == "GRANT SELECT ON `ds`.* TO 'shop_ro'@'%'"
    assert grants[1].startswith(f"GRANT {accounts.APP_DATABASE_RIGHTS} ON `dataagent_ds`.*")
    assert len(grants) == 2
    # The account exists before the app tables are made in its database.
    assert conn.sql.index(grants[1]) < conn.sql.index("USE `dataagent_ds`")
