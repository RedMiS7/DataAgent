"""Explain a failed or partial answer to the user: what went wrong and what to try."""

from __future__ import annotations

import re

from ..api.schemas import Failure

UNKNOWN_COLUMN = re.compile(r"Unknown column '([^']+)'")
UNKNOWN_TABLE = re.compile(r"Table '([^']+)' doesn't exist")
# MySQL stops a statement past max_execution_time with error 3024.
QUERY_TIMEOUT = re.compile(r"\b3024\b|maximum statement execution time", re.IGNORECASE)
SQL_PREFIXES = ("SQL 校验失败：", "SQL 执行失败：")
DETAIL_LIMIT = 200

# Exceptions from these packages come from calling the model service.
MODEL_MODULES = ("openai", "httpx", "httpcore", "dashscope", "anthropic", "agentscope.model")

RETRY_LATER = "可以稍后重试，或者把问题拆小一点再问。"
REPHRASE = "可以换个说法，或者写明要看的指标、维度和时间，例如“按渠道看上个月的 GMV”。"
CHECK_MODEL = "可以到设置页检查模型配置和 Key，或者稍后重试。"


def explain_sql_error(message: str) -> str:
    """The database or validator error in plain words, keeping the useful part."""
    if match := UNKNOWN_COLUMN.search(message):
        return f"数据库里没有“{match.group(1)}”这个字段。"
    if match := UNKNOWN_TABLE.search(message):
        return f"数据库里没有“{match.group(1)}”这张表。"
    if QUERY_TIMEOUT.search(message):
        return "查询超过了数据库的执行时间上限。"
    text = message
    for prefix in SQL_PREFIXES:
        text = text.removeprefix(prefix)
    # SQLAlchemy appends the statement and a link; the statement is shown elsewhere.
    text = text.split("\n[SQL:")[0].split("\n(Background")[0].strip()
    return text[:DETAIL_LIMIT]


def is_model_error(exc: BaseException) -> bool:
    """Whether an exception, or one that caused it, came from the model service."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__module__.startswith(MODEL_MODULES):
            return True
        current = current.__cause__ or current.__context__
    return False


def timeout_failure(seconds: float, has_results: bool) -> Failure:
    limit = f"{seconds:.0f} 秒"
    message = (f"超过 {limit} 还没有完成，下面是已经查到的数据。" if has_results
               else f"超过 {limit} 还没有查到数据。")
    return Failure(kind="timeout", message=message, suggestion=RETRY_LATER)


def sql_failure(attempts: int, last_error: str) -> Failure:
    reason = explain_sql_error(last_error)
    return Failure(kind="sql_failed",
                   message=f"SQL 试了 {attempts} 次都没有成功。最后一次的原因：{reason}",
                   suggestion=REPHRASE)


def model_failure(exc: BaseException | None, has_results: bool) -> Failure:
    detail = f"（{type(exc).__name__}: {exc}）"[:DETAIL_LIMIT] if exc is not None else ""
    message = (f"调用大模型时出错{detail}，下面是已经查到的数据。" if has_results
               else f"调用大模型时出错{detail}。")
    return Failure(kind="model_error", message=message, suggestion=CHECK_MODEL)


def not_configured_failure(message: str) -> Failure:
    return Failure(kind="model_error", message=message, suggestion="到设置页填写大模型的 Key。")


def unexpected_failure(exc: BaseException, has_results: bool) -> Failure:
    detail = f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT]
    message = (f"处理时出现异常（{detail}），下面是已经查到的数据。" if has_results
               else f"处理时出现异常（{detail}）。")
    return Failure(kind="unexpected", message=message,
                   suggestion="可以重试；一直出现时请查看后端日志。")


def no_answer_failure() -> Failure:
    return Failure(kind="no_answer", message="模型没有查询数据，也没有给出可用的回答。",
                   suggestion=REPHRASE)


def step_failure(goals: list[str]) -> Failure:
    names = "；".join(goals)
    return Failure(kind="step_failed",
                   message=f"有 {len(goals)} 步没有查到数据：{names}。结论缺少这部分。",
                   suggestion="可以重新生成，或者单独问这一部分。")


def exception_failure(exc: BaseException, has_results: bool) -> Failure:
    if is_model_error(exc):
        return model_failure(exc, has_results)
    return unexpected_failure(exc, has_results)


__all__ = [
    "exception_failure",
    "explain_sql_error",
    "is_model_error",
    "no_answer_failure",
    "not_configured_failure",
    "sql_failure",
    "step_failure",
    "timeout_failure",
]
