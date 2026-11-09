"""Load CSV and Excel files into MySQL, as a manifest describes them.

Each table goes into ``raw_database`` with all of its columns. A view of the same name in
``database`` leaves out the ``hidden`` columns, and the read-only account may only read
those views. ``app_database`` gets the application tables, so the semantic layer of the
dataset can be synced into it, and the app account gets its rights there. Running the
import again replaces the data. It runs with the admin account.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from .config import Settings
from .db import accounts

NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
TYPE = re.compile(r"^(?:date|int|bool|varchar\((\d+)\)|decimal\((\d+),(\d+)\))$")
DATE_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")
TRUE_WORDS = {"是", "1", "true", "yes"}
FALSE_WORDS = {"否", "0", "false", "no"}
BATCH = 2000


class DatasetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Column:
    # The header in the source file.
    source: str
    name: str
    type: str


@dataclass(slots=True)
class Table:
    name: str
    source: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)
    indexes: list[list[str]] = field(default_factory=list)
    # Columns kept out of the view the read-only account reads, such as phone numbers.
    hidden: list[str] = field(default_factory=list)
    # The worksheet of an Excel source; the first one when empty.
    sheet: str = ""

    @property
    def visible(self) -> list[Column]:
        return [column for column in self.columns if column.name not in self.hidden]


@dataclass(slots=True)
class Manifest:
    raw_database: str
    database: str
    app_database: str
    data_dir: Path
    tables: list[Table]


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or not NAME.match(value):
        raise DatasetError(f"{what}只能用小写字母、数字和下划线：{value!r}")
    return value


def load_manifest(path: Path) -> Manifest:
    """Read and check a manifest; paths in it are relative to the manifest's folder."""
    data = json.loads(path.read_text(encoding="utf-8"))
    tables: list[Table] = []
    for raw in data["tables"]:
        table = Table(
            name=_name(raw["name"], "表名"), source=raw["source"],
            columns=[Column(column["source"].strip(), _name(column["name"], "字段名"),
                            column["type"].strip().lower())
                     for column in raw["columns"]],
            primary_key=list(raw.get("primary_key", [])),
            indexes=[list(index) for index in raw.get("indexes", [])],
            hidden=list(raw.get("hidden", [])), sheet=raw.get("sheet", ""),
        )
        names = [column.name for column in table.columns]
        if len(set(names)) != len(names):
            raise DatasetError(f"表 {table.name} 有重名的字段")
        for column in table.columns:
            if not TYPE.match(column.type):
                raise DatasetError(f"{table.name}.{column.name} 的类型不支持：{column.type}")
        for listed in [table.primary_key, table.hidden, *table.indexes]:
            unknown = [name for name in listed if name not in names]
            if unknown:
                raise DatasetError(f"表 {table.name} 没有这些字段：{'、'.join(unknown)}")
        tables.append(table)
    if len({table.name for table in tables}) != len(tables):
        raise DatasetError("清单里有重名的表")
    return Manifest(
        raw_database=_name(data["raw_database"], "库名"),
        database=_name(data["database"], "库名"),
        app_database=_name(data["app_database"], "库名"),
        data_dir=(path.parent / data.get("data_dir", ".")).resolve(),
        tables=tables,
    )


def sql_type(kind: str) -> str:
    return {"date": "DATE", "int": "BIGINT", "bool": "TINYINT"}.get(kind, kind.upper())


def convert(value: Any, kind: str) -> Any:
    """A source value as the column stores it; None for an empty cell.

    Raises ValueError with what is wrong; the caller adds where it is.
    """
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == "":
        return None
    if kind == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        for pattern in DATE_FORMATS:
            try:
                return datetime.strptime(str(value), pattern).date()
            except ValueError:
                continue
        raise ValueError(f"不是日期：{value!r}")
    if kind == "bool":
        word = str(value).lower()
        if word in TRUE_WORDS:
            return 1
        if word in FALSE_WORDS:
            return 0
        raise ValueError(f"不是是/否：{value!r}")
    if kind == "int":
        try:
            number = value if isinstance(value, (int, float)) else float(str(value))
        except ValueError as exc:
            raise ValueError(f"不是整数：{value!r}") from exc
        if isinstance(number, bool) or float(number) != int(number):
            raise ValueError(f"不是整数：{value!r}")
        return int(number)
    match = TYPE.match(kind)
    assert match is not None
    length, precision, scale = match.groups()
    if length:
        textual = value.isoformat() if isinstance(value, (date, datetime)) else str(value)
        if len(textual) > int(length):
            raise ValueError(f"超过 {length} 个字符：{textual!r}")
        return textual
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"不是数字：{value!r}") from exc
    if abs(number) >= Decimal(10) ** (int(precision) - int(scale)):
        raise ValueError(f"超出 {kind} 的范围：{value!r}")
    return number


def _read(path: Path, sheet: str) -> tuple[list[str], Iterator[list[Any]]]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        # data_only: the values Excel computed, not the formulas behind them.
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
        rows = worksheet.iter_rows(values_only=True)
        header = [str(cell).strip() if cell is not None else "" for cell in next(rows)]
        return header, (list(row) for row in rows)
    handle = path.open(encoding="utf-8-sig", newline="")
    reader = csv.reader(handle)
    header = [cell.strip() for cell in next(reader)]

    def rows() -> Iterator[list[Any]]:
        with handle:
            yield from reader
    return header, rows()


def table_rows(table: Table, data_dir: Path) -> Iterator[dict[str, Any]]:
    """The rows of a table's source file, converted to the column types."""
    path = data_dir / table.source
    if not path.exists():
        raise DatasetError(f"找不到数据文件：{path}")
    header, rows = _read(path, table.sheet)
    missing = [column.source for column in table.columns if column.source not in header]
    if missing:
        raise DatasetError(f"{table.source} 里没有这些列：{'、'.join(missing)}")
    positions = [(column, header.index(column.source)) for column in table.columns]
    for line, row in enumerate(rows, start=2):
        if not any(cell not in (None, "") for cell in row):
            continue
        converted: dict[str, Any] = {}
        for column, position in positions:
            value = row[position] if position < len(row) else None
            try:
                converted[column.name] = convert(value, column.type)
            except ValueError as exc:
                raise DatasetError(f"{table.source} 第 {line} 行，{column.source}：{exc}") from exc
            if converted[column.name] is None and column.name in table.primary_key:
                raise DatasetError(f"{table.source} 第 {line} 行，{column.source}：主键不能为空")
        yield converted


def create_table_sql(database: str, table: Table) -> str:
    lines = [f"`{column.name}` {sql_type(column.type)} "
             + ("NOT NULL" if column.name in table.primary_key else "NULL")
             for column in table.columns]
    if table.primary_key:
        lines.append(f"PRIMARY KEY ({', '.join(f'`{name}`' for name in table.primary_key)})")
    for index in table.indexes:
        lines.append(f"KEY `idx_{'_'.join(index)}` ({', '.join(f'`{name}`' for name in index)})")
    body = ",\n    ".join(lines)
    return (f"CREATE TABLE `{database}`.`{table.name}` (\n    {body}\n) ENGINE=InnoDB "
            "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci")


def view_sql(raw_database: str, database: str, table: Table) -> str:
    columns = ", ".join(f"`{column.name}`" for column in table.visible)
    return (f"CREATE OR REPLACE VIEW `{database}`.`{table.name}` AS "
            f"SELECT {columns} FROM `{raw_database}`.`{table.name}`")


def app_schema_statements(path: Path) -> list[str]:
    """The table statements of the app schema, without its own database and USE lines."""
    # Comments go first: one of them holds a semicolon.
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if not line.strip().startswith("--")]
    statements = []
    for statement in "\n".join(lines).split(";"):
        body = statement.strip()
        if body and not re.match(r"(?i)^(CREATE DATABASE|USE)\b", body):
            statements.append(body)
    return statements


def _insert(conn: Connection, database: str, table: Table, rows: Iterator[dict[str, Any]],
            on_progress: Callable[[str, int], None]) -> int:
    names = [column.name for column in table.columns]
    statement = text(f"INSERT INTO `{database}`.`{table.name}` "
                     f"({', '.join(f'`{name}`' for name in names)}) "
                     f"VALUES ({', '.join(f':{name}' for name in names)})")
    count = 0
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH:
            conn.execute(statement, batch)
            count += len(batch)
            batch = []
            on_progress(table.name, count)
    if batch:
        conn.execute(statement, batch)
        count += len(batch)
    on_progress(table.name, count)
    return count


def import_dataset(settings: Settings, manifest: Manifest, app_schema: Path,
                   on_progress: Callable[[str, int], None] = lambda table, rows: None
                   ) -> dict[str, int]:
    """Create the databases, load every table, build the views and grant read access."""
    reader = _name(settings.mysql_ro_user, "只读账号")
    engine = create_engine(settings.admin_server_url.render_as_string(hide_password=False))
    counts: dict[str, int] = {}
    try:
        with engine.begin() as conn:
            for database in (manifest.raw_database, manifest.database, manifest.app_database):
                conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{database}` DEFAULT "
                                  "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"))
        for table in manifest.tables:
            # Read the whole file first: a bad value stops before the old table is dropped.
            rows = list(table_rows(table, manifest.data_dir))
            with engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS `{manifest.raw_database}`.`{table.name}`"))
                conn.execute(text(create_table_sql(manifest.raw_database, table)))
                counts[table.name] = _insert(conn, manifest.raw_database, table, iter(rows),
                                             on_progress)
                stored = conn.execute(text(
                    f"SELECT COUNT(*) FROM `{manifest.raw_database}`.`{table.name}`")).scalar()
                if stored != counts[table.name]:
                    raise DatasetError(f"{table.name} 写入 {counts[table.name]} 行，"
                                       f"表里只有 {stored} 行")
                conn.execute(text(view_sql(manifest.raw_database, manifest.database, table)))
        with engine.begin() as conn:
            conn.execute(text(f"GRANT SELECT ON `{manifest.database}`.* TO '{reader}'@'%'"))
            # The instance serving this dataset runs with the app account, not the admin one.
            accounts.ensure_app_user(conn, settings.mysql_app_user, settings.mysql_app_password,
                                     manifest.app_database)
            conn.execute(text(f"USE `{manifest.app_database}`"))
            for statement in app_schema_statements(app_schema):
                conn.execute(text(statement))
            # Evaluation reads this dataset with its own account and writes the evaluation
            # tables made just above; skipped until it has a password, and grant_eval_user
            # does it later.
            if settings.mysql_eval_password:
                accounts.ensure_eval_user(conn, settings.mysql_eval_user,
                                          settings.mysql_eval_password, manifest.database,
                                          manifest.app_database)
    finally:
        engine.dispose()
    return counts
