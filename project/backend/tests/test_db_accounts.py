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


# The evaluation account: reads a dataset's views and the app database, nothing else.

def test_evaluation_connects_with_its_own_account(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mysql_eval_user="judge", mysql_eval_password="pw",
                             mysql_business_db="safe", mysql_app_db="dataagent")
    assert settings.mysql_eval_password == "pw"
    own = settings.eval_business_url()
    assert (own.username, own.password, own.database) == ("judge", "pw", "safe")
    assert settings.eval_business_url("dabench").database == "dabench"
    assert (settings.eval_app_url().username, settings.eval_app_url().database) == (
        "judge", "dataagent")
    # The agent's queries keep the read-only account.
    assert settings.business_db_url.username == "shop_ro"


def test_the_evaluation_account_has_a_name_but_no_default_password(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert settings.mysql_eval_user == "dataagent_eval"
    assert settings.mysql_eval_password == ""


def test_eval_grants_only_read_the_views_and_the_app_database() -> None:
    assert accounts.eval_grant_statements("judge", "dabench", "dataagent_dabench") == [
        "GRANT SELECT ON `dabench`.* TO 'judge'@'%'",
        "GRANT SELECT ON `dataagent_dabench`.* TO 'judge'@'%'"]


def test_ensure_eval_user_binds_the_password_and_refuses_an_empty_one() -> None:
    conn = RecordingConnection()
    statements = accounts.ensure_eval_user(conn, "judge", "p'w", "safe", "dataagent")  # type: ignore[arg-type]
    assert conn.sql[0] == "CREATE USER IF NOT EXISTS 'judge'@'%' IDENTIFIED BY :password"
    assert conn.calls[0][1] == {"password": "p'w"}
    assert conn.sql[2:] == statements
    with pytest.raises(ValueError, match="MYSQL_EVAL_PASSWORD"):
        accounts.ensure_eval_user(RecordingConnection(), "judge", "", "safe", "dataagent")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="只能用小写字母"):
        accounts.ensure_eval_user(RecordingConnection(), "judge", "pw", "safe`.*", "dataagent")  # type: ignore[arg-type]


def test_the_init_script_keeps_no_evaluation_password() -> None:
    script = (PROJECT_DIR / "data" / "initdb" / "03_grants.sql").read_text(encoding="utf-8")
    statements = [line for line in script.splitlines() if not line.startswith("--")]
    assert not any("dataagent_eval" in line for line in statements)
    assert "grant_eval_user" in script


def test_grant_eval_user_grants_this_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    conn = RecordingConnection()
    engine = RecordingEngine(conn)
    settings = make_settings(tmp_path, mysql_business_db="dabench",
                             mysql_app_db="dataagent_dabench", mysql_eval_password="secret")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_engine", lambda url: engine)
    assert cli.main(["grant_eval_user"]) == 0
    assert [sql for sql in conn.sql if sql.startswith("GRANT")] == [
        "GRANT SELECT ON `dabench`.* TO 'dataagent_eval'@'%'",
        "GRANT SELECT ON `dataagent_dabench`.* TO 'dataagent_eval'@'%'"]
    assert engine.disposed
    output = capsys.readouterr().out
    assert "dataagent_eval" in output and "secret" not in output


def test_grant_eval_user_without_a_password_says_what_to_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(cli, "get_settings", lambda: make_settings(tmp_path))
    monkeypatch.setattr(cli, "create_engine", lambda url: RecordingEngine(conn))
    assert cli.main(["grant_eval_user"]) == 1
    assert "MYSQL_EVAL_PASSWORD" in capsys.readouterr().err
    assert conn.sql == []


def test_importing_a_dataset_lets_the_evaluation_account_read_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(importer, "create_engine", lambda url: RecordingEngine(conn))
    manifest = Manifest(raw_database="ds_raw", database="ds", app_database="dataagent_ds",
                        data_dir=tmp_path, tables=[])
    importer.import_dataset(make_settings(tmp_path, mysql_eval_password="pw"), manifest,
                            PROJECT_DIR / "data" / "initdb" / "02_app_schema.sql")
    grants = [sql for sql in conn.sql if sql.startswith("GRANT")]
    assert grants[2:] == ["GRANT SELECT ON `ds`.* TO 'dataagent_eval'@'%'",
                          "GRANT SELECT ON `dataagent_ds`.* TO 'dataagent_eval'@'%'"]
    # The raw database is never granted to it.
    assert not any("ds_raw" in grant for grant in grants)


class _EngineSpy:
    """Stands in for engine._engine: records the URL each connection uses."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.session: list[str] = []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        spy = self

        class _Conn:
            def exec_driver_sql(self, sql: str) -> None:
                spy.session.append(sql)

        class _Engine:
            @contextmanager
            def connect(self) -> Any:
                yield _Conn()

        return _Engine()


def test_eval_connections_use_the_evaluation_account_with_a_time_limit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import engine
    spy = _EngineSpy()
    monkeypatch.setattr(engine, "_engine", spy)
    settings = make_settings(tmp_path, mysql_eval_password="pw", sql_timeout_ms=7000)
    with engine.eval_business_connection(settings, "dabench"):
        pass
    with engine.eval_app_connection(settings):
        pass
    assert spy.urls[0].startswith("mysql+pymysql://dataagent_eval:pw@")
    assert "/dabench?" in spy.urls[0]
    assert spy.urls[1].startswith("mysql+pymysql://dataagent_eval:pw@")
    assert "/dataagent?" in spy.urls[1]
    assert spy.session == ["SET SESSION max_execution_time = 7000"]
