"""The MySQL account the service runs with.

The service reads and writes its app database with ``MYSQL_APP_USER``. It may create
tables and add columns there, as start-up adds the ones older databases lack, but it
cannot drop tables or grant rights. When demo data is on, it may also read and insert
into the raw shop database. Admin commands create the account with ``MYSQL_ADMIN_USER``.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

APP_DATABASE_RIGHTS = "SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX"
DEMO_DATABASE_RIGHTS = "SELECT, INSERT"


def checked_name(value: str, what: str) -> str:
    """Names go into SQL as identifiers, so only plain lowercase names are allowed."""
    if not NAME.match(value):
        raise ValueError(f"{what}只能用小写字母、数字和下划线：{value!r}")
    return value


def grant_statements(user: str, app_database: str, demo_database: str | None) -> list[str]:
    user = checked_name(user, "应用账号名")
    statements = [f"GRANT {APP_DATABASE_RIGHTS} ON `{checked_name(app_database, '应用库名')}`.* "
                  f"TO '{user}'@'%'"]
    if demo_database:
        statements.append(f"GRANT {DEMO_DATABASE_RIGHTS} ON "
                          f"`{checked_name(demo_database, '演示数据库名')}`.* TO '{user}'@'%'")
    return statements


def ensure_app_user(conn: Connection, user: str, password: str, app_database: str,
                    demo_database: str | None = None) -> list[str]:
    """Create the account or reset its password, then grant its rights. Safe to repeat.

    Runs on a connection of the admin account. Returns the GRANT statements it ran.
    """
    if not password:
        raise ValueError("应用账号的密码为空，请设置 MYSQL_APP_PASSWORD")
    name = checked_name(user, "应用账号名")
    # The password is a bound value: the driver quotes it, so any character is safe.
    conn.execute(text(f"CREATE USER IF NOT EXISTS '{name}'@'%' IDENTIFIED BY :password"),
                 {"password": password})
    conn.execute(text(f"ALTER USER '{name}'@'%' IDENTIFIED BY :password"),
                 {"password": password})
    statements = grant_statements(name, app_database, demo_database)
    for statement in statements:
        conn.execute(text(statement))
    return statements
