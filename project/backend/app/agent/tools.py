from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from ..config import Settings
from ..db.engine import app_connection, business_connection
from ..nl2sql.executor import ExecutionResult, SQLExecutionError, execute_sql
from ..safety.validator import (
    Columns,
    SQLPolicy,
    SQLValidationError,
    unknown_column_hint,
    validate_and_limit,
)
from ..semantic import repository
from ..semantic.models import RetrievalBundle
from ..semantic.retriever import SemanticRetriever
from ..semantic.schema_context import (
    SchemaContext,
    TableColumns,
    bare_name,
    build_schema_context,
    render_table,
    table_names,
)
from .trace import CallTrace

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, dict[str, Any]], None]

DECLINE_REASON_LIMIT = 200
DEFAULT_DECLINE_REASON = "这个请求无法执行。"
ANALYSIS_MAX_QUERIES = 10
QUERY_LIMIT_MESSAGE = "查询次数已用完，不要再调用 run_sql，请基于已有结果作答。"
STEP_LIMIT_MESSAGE = "这一步的查询次数已用完，不要再调用 run_sql，用一两句话说明这一步看到了什么。"
UNKNOWN_COLUMN_HINT = "字段名不对。先调用 describe_table 查看这张表的真实字段，再重写 SQL。"
FAILURE_LIMIT_MESSAGE = ("SQL 已经连续失败 {count} 次。不要再调用 run_sql，"
                         "直接告诉用户这次没有查出来，并说明最后一次失败的原因。")
MYSQL_UNKNOWN_COLUMN = re.compile(r"Unknown column '([^']+)'")

# The real columns of each business database, read on first use. A schema change
# needs a restart, like the semantic layer.
_columns_cache: dict[str, dict[str, list[tuple[str, str]]]] = {}
_columns_lock = threading.Lock()

READ_ONLY_PERMISSION = PermissionDecision(
    behavior=PermissionBehavior.ALLOW,
    message="read-only analysis tool",
)


@dataclass(slots=True)
class AgentRunContext:
    settings: Settings
    retriever: SemanticRetriever
    emit: EmitFn = lambda event, data: None
    bundle: RetrievalBundle | None = None
    mode: str = "query"
    log_tag: str = "-"
    executions: list[ExecutionResult] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)
    # The plan step of each execution; None outside a plan.
    step_ids: list[int | None] = field(default_factory=list)
    # Queries running or done. Plan steps run at once, so the limit counts queries in flight.
    reserved: int = 0
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    # The last SQL that failed, shown when no query succeeds.
    failed_sql: str = ""
    trace: CallTrace = field(default_factory=CallTrace)
    declined: str = ""


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _one_line(sql: str) -> str:
    return " ".join(sql.split())


def _database_error(exc: SQLExecutionError) -> str:
    """The database's own message, without SQLAlchemy's copy of the statement."""
    return str(getattr(exc.__cause__, "orig", None) or exc)


def describe_tables(settings: Settings, names: list[str]) -> str:
    """List real columns, adding descriptions where the semantic layer has them.

    The semantic layer leaves out some columns, including join keys, so it cannot
    be the list of what exists.
    """
    with business_connection(settings) as conn:
        columns = repository.fetch_table_columns(conn)
    with app_connection(settings) as conn:
        items = {(item.table_name, item.column_name): item
                 for item in repository.fetch_schema_items(conn, enabled_only=False)}

    prefix = f"{settings.mysql_business_db}."
    sections: list[str] = []
    unknown: list[str] = []
    for name in names:
        table = name.strip().strip("`").removeprefix(prefix)
        if table not in columns:
            unknown.append(name)
            continue
        sections.append(render_table(table, columns[table], items, prefix))
    if unknown:
        sections.append(f"没有这些表：{'、'.join(unknown)}。可用的表：{'、'.join(sorted(columns))}")
    return "\n\n".join(sections)


def _make_describe(context: AgentRunContext) -> Callable[[list[str]], Any]:
    async def describe_table(tables: list[str]) -> str:
        """查看表的全部字段、类型和说明。写 SQL 前字段不确定时调用。

        Args:
            tables: 要查看的表名，可以一次查多张，例如 ["ad_daily_stats", "refunds"]。
        """
        names = "、".join(tables)
        context.emit("status", {"step": "describing", "message": f"查看字段：{names}"})
        logger.info("[%s] 查看字段 tables=%s", context.log_tag, names)
        try:
            # In a thread, so plan steps running at once are not held up by the database.
            return await asyncio.to_thread(describe_tables, context.settings, tables)
        except SQLAlchemyError as exc:
            logger.warning("[%s] 查看字段失败 tables=%s error=%s", context.log_tag, names, exc)
            return f"查看字段失败：{exc}"

    return describe_table


def _make_search(context: AgentRunContext) -> Callable[[str], str]:
    def search_semantic(question: str) -> str:
        """检索问题相关的表、字段、指标口径与历史案例。

        Args:
            question: 需要检索的自然语言问题。
        """
        bundle = context.retriever.retrieve(question)
        context.bundle = bundle
        return bundle.to_prompt_context()

    return search_semantic


def table_columns(settings: Settings) -> TableColumns | None:
    """The real columns and types of the business database, as {table: [(column, type)]};
    None when they cannot be read."""
    key = f"{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_business_db}"
    with _columns_lock:
        if key in _columns_cache:
            return _columns_cache[key]
        try:
            with business_connection(settings) as conn:
                columns = repository.fetch_table_columns(conn)
        except SQLAlchemyError as exc:
            logger.warning("读取字段失败 error=%s", exc)
            return None
        _columns_cache[key] = columns
        return columns


def known_columns(settings: Settings) -> Columns | None:
    """The real column names, as {table: [column]}; None when they cannot be read, and
    SQL then runs without the column check."""
    columns = table_columns(settings)
    if columns is None:
        return None
    return {table: [name for name, _ in items] for table, items in columns.items()}


def sql_policy(settings: Settings) -> SQLPolicy:
    """What SQL may read: the tables the read-only account sees in the business database."""
    columns = known_columns(settings)
    if columns is None:
        logger.warning("读不到业务库的表和字段，这次只检查库名，不检查表名和字段")
    return SQLPolicy(schema=settings.mysql_business_db, dialect=settings.sql_dialect,
                     columns=columns)


def schema_context(settings: Settings, conn: Connection, bundle: RetrievalBundle,
                   wanted: list[str]) -> SchemaContext | None:
    """The tables for the prompt, each with all of its columns; None when the columns
    cannot be read, and the prompt keeps the retrieved columns instead.

    ``wanted`` are tables from outside retrieval, such as those of the recent SQL; they
    come after what retrieval found.
    """
    columns = table_columns(settings)
    if not columns:
        return None
    prefix = f"{settings.mysql_business_db}."
    items = repository.fetch_schema_items(conn, enabled_only=False)
    tables = retrieved_tables(bundle, settings.mysql_business_db) + wanted
    summaries = {bare_name(table.table_name, prefix): table.summary for table in bundle.tables}
    return build_schema_context(columns, items, summaries, tables,
                                settings.schema_token_budget, prefix)


def retrieved_tables(bundle: RetrievalBundle, schema: str) -> list[str]:
    """The tables retrieval points to, without the schema prefix, most direct first:
    those of the matched columns, then the matched metrics' tables, then the tables the
    matched cases' SQL reads."""
    prefix = f"{schema}."
    tables = [bare_name(hit.item.table_name, prefix) for hit in bundle.schema_items]
    for hit in bundle.metrics:
        tables.extend(bare_name(part, prefix) for part in hit.metric.related_tables.split(",")
                      if part.strip())
    for case in bundle.cases:
        tables.extend(table_names(case.sql_text, schema))
    return list(dict.fromkeys(tables))




def _column_hint(settings: Settings, sql: str, message: str) -> str:
    """Close matches and the real columns for MySQL's "Unknown column" error."""
    match = MYSQL_UNKNOWN_COLUMN.search(message)
    columns = known_columns(settings)
    if match is None or not columns:
        return ""
    return unknown_column_hint(sql, match.group(1), columns, settings.sql_dialect)


def _make_run_sql(context: AgentRunContext, step_id: int | None = None,
                  step_limit: int | None = None) -> Callable[..., Any]:
    step_reserved = 0
    # Failures since the last query that ran; past the limit the model must stop trying.
    failures_in_row = 0

    async def run_sql(sql: str, purpose: str = "") -> str:
        """执行一条只读 SQL 并返回结果预览。

        Args:
            sql: 单条只读 SELECT 语句，只能查询业务库里的表。
            purpose: 这条查询要看什么，一句中文，例如“本期与上期整体对比”。
        """
        nonlocal step_reserved, failures_in_row
        analysis = context.mode == "analysis"
        failure_limit = context.settings.sql_max_retry
        if failures_in_row >= failure_limit:
            logger.info("[%s] SQL 已连续失败 %d 次，未执行 sql=%s", context.log_tag,
                        failures_in_row, _one_line(sql))
            return FAILURE_LIMIT_MESSAGE.format(count=failures_in_row)
        if step_limit is not None and step_reserved >= step_limit:
            logger.info("[%s] 第 %s 步查询次数已用完，未执行 sql=%s", context.log_tag, step_id,
                        _one_line(sql))
            return STEP_LIMIT_MESSAGE
        if analysis and context.reserved >= ANALYSIS_MAX_QUERIES:
            logger.info("[%s] 查询次数已用完，未执行 sql=%s", context.log_tag, _one_line(sql))
            return QUERY_LIMIT_MESSAGE

        purpose = purpose.strip()
        # The number is fixed once the query succeeds; steps running at once finish in any order.
        attempt = len(context.executions) + 1
        prefix = f"第{step_id}步 · " if step_id is not None else ""

        context.attempts += 1
        if not analysis:
            context.emit("status", {"step": "validating", "message": "正在校验 SQL 安全性"})
        policy = await asyncio.to_thread(sql_policy, context.settings)
        try:
            safe_sql = validate_and_limit(sql, context.settings.sql_max_rows, policy)
        except SQLValidationError as exc:
            message = str(exc)
            context.errors.append(message)
            context.failed_sql = sql
            failures_in_row += 1
            logger.warning("[%s] 查询 %d 被拦截 reason=%s sql=%s", context.log_tag, attempt,
                           _one_line(message), _one_line(sql))
            context.emit("status", {"step": "retrying",
                                    "message": f"{prefix}SQL 被拦截：{message.splitlines()[0]}"})
            return _with_limit(f"SQL 校验失败：{message}", failures_in_row, failure_limit)

        if not analysis:
            context.emit("status", {"step": "executing", "message": "正在执行查询"})
        context.reserved += 1
        step_reserved += 1
        try:
            # In a thread, so plan steps running at once are not held up by the database.
            execution = await asyncio.to_thread(execute_sql, context.settings, safe_sql)
        except SQLExecutionError as exc:
            context.reserved -= 1
            step_reserved -= 1
            message = str(exc)
            context.errors.append(message)
            context.failed_sql = safe_sql.sql
            logger.warning("[%s] 查询 %d 失败 error=%s sql=%s", context.log_tag, attempt,
                           _database_error(exc), _one_line(safe_sql.sql))
            failures_in_row += 1
            context.emit("status", {"step": "retrying", "message": f"{prefix}{message}"})
            if "Unknown column" in message:
                hint = await asyncio.to_thread(_column_hint, context.settings, safe_sql.sql,
                                               message)
                message = f"{message}\n{hint or UNKNOWN_COLUMN_HINT}"
            return _with_limit(message, failures_in_row, failure_limit)

        failures_in_row = 0
        context.executions.append(execution)
        context.purposes.append(purpose)
        context.step_ids.append(step_id)
        index = len(context.executions)
        logger.info("[%s] 查询 %d 成功 %dms %d 行 目的=%s", context.log_tag, index,
                    execution.duration_ms, len(execution.rows), purpose or "-")
        logger.debug("[%s] 查询 %d sql=%s", context.log_tag, index, _one_line(execution.sql))
        preview = {
            "columns": execution.columns,
            "rows": execution.rows[:50],
            "row_count": len(execution.rows),
            "truncated": execution.truncated,
            "duration_ms": execution.duration_ms,
        }
        if analysis:
            # One line per query, so the panel lists what each query was for.
            context.emit("status", {
                "step": f"query_{index}",
                "message": f"{prefix}查询 {index}：{purpose or '查询数据'}，返回 {len(execution.rows)} 行",
            })
            preview["query_index"] = index
        return _json(preview)

    return run_sql


def _with_limit(message: str, failures: int, limit: int) -> str:
    """The error, and at the limit, the instruction to stop trying."""
    if failures < limit:
        return message
    return f"{message}\n{FAILURE_LIMIT_MESSAGE.format(count=failures)}"


def _make_decline(context: AgentRunContext) -> Callable[[str], str]:
    def decline_request(reason: str) -> str:
        """拒绝执行不该执行的请求，并说明原因。

        Args:
            reason: 给用户看的一句中文原因。
        """
        context.declined = reason.strip()[:DECLINE_REASON_LIMIT] or DEFAULT_DECLINE_REASON
        logger.info("[%s] 拒绝执行 reason=%s", context.log_tag, context.declined)
        context.emit("status", {"step": "declined", "message": "已拒绝执行这个请求"})
        return "已记录拒绝原因。不要再调用 run_sql，用一句话告知用户原因。"

    return decline_request


def build_toolkit(context: AgentRunContext, step_id: int | None = None,
                  step_limit: int | None = None) -> Toolkit:
    """Tools for one agent; a plan step's toolkit tags its queries and caps their number."""
    return Toolkit([
        FunctionTool(_make_search(context), name="search_semantic",
                     description="检索与问题相关的表、字段、指标口径和历史案例",
                     permission=READ_ONLY_PERMISSION, is_read_only=True),
        FunctionTool(_make_run_sql(context, step_id, step_limit), name="run_sql",
                     description="执行一条只读 SELECT 语句并返回查询结果",
                     permission=READ_ONLY_PERMISSION, is_read_only=True),
        FunctionTool(_make_decline(context), name="decline_request",
                     description="请求含注入写法、要求改数据或绕过规则时，拒绝执行并说明原因",
                     permission=READ_ONLY_PERMISSION, is_read_only=True),
        FunctionTool(_make_describe(context), name="describe_table",
                     description="查看表的全部字段、类型和说明",
                     permission=READ_ONLY_PERMISSION, is_read_only=True),
    ])


def build_breakdown_toolkit(context: AgentRunContext,
                            breakdown: Callable[[str], dict[str, Any]]) -> Toolkit:
    def refund_breakdown(dimension: str) -> str:
        """按维度返回当前期与对比期的退款金额对比。

        Args:
            dimension: 下钻维度，取 product、channel 或 carrier。
        """
        return _json(breakdown(dimension))

    return Toolkit([
        FunctionTool(refund_breakdown, name="refund_breakdown",
                     description="按商品/渠道/承运商返回退款金额变化",
                     permission=READ_ONLY_PERMISSION, is_read_only=True),
    ])