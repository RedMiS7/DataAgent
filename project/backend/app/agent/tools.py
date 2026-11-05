from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit

from ..config import Settings
from ..nl2sql.executor import ExecutionResult, SQLExecutionError, execute_sql
from ..safety.validator import SQLValidationError, validate_and_limit
from ..semantic.models import RetrievalBundle
from ..semantic.retriever import SemanticRetriever

EmitFn = Callable[[str, dict[str, Any]], None]

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
    sql: str = ""
    execution: ExecutionResult | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


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


def _make_run_sql(context: AgentRunContext) -> Callable[[str], str]:
    def run_sql(sql: str) -> str:
        """执行一条只读 SQL 并返回结果预览。

        Args:
            sql: 单条 MySQL SELECT 语句，只能查询 safe 库。
        """
        context.attempts += 1
        context.emit("status", {"step": "validating", "message": "正在校验 SQL 安全性"})
        try:
            safe_sql = validate_and_limit(sql, context.settings.sql_max_rows)
        except SQLValidationError as exc:
            message = str(exc)
            context.errors.append(message)
            context.emit("status", {"step": "retrying", "message": f"SQL 被拦截：{message}"})
            return f"SQL 校验失败：{message}"

        context.emit("status", {"step": "executing", "message": "正在执行查询"})
        try:
            execution = execute_sql(context.settings, safe_sql)
        except SQLExecutionError as exc:
            context.errors.append(str(exc))
            context.emit("status", {"step": "retrying", "message": f"SQL 执行失败：{exc}"})
            return f"SQL 执行失败：{exc}"

        context.sql = safe_sql.sql
        context.execution = execution
        return _json({
            "columns": execution.columns,
            "rows": execution.rows[:50],
            "row_count": len(execution.rows),
            "truncated": execution.truncated,
            "duration_ms": execution.duration_ms,
        })

    return run_sql


def build_toolkit(context: AgentRunContext) -> Toolkit:
    return Toolkit([
        FunctionTool(_make_search(context), name="search_semantic",
                     description="检索与问题相关的表、字段、指标口径和历史案例",
                     permission=READ_ONLY_PERMISSION, is_read_only=True),
        FunctionTool(_make_run_sql(context), name="run_sql",
                     description="执行一条只读 SELECT 语句并返回查询结果",
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