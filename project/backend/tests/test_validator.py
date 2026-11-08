from __future__ import annotations

import pytest
from app.safety.validator import SQLPolicy, SQLValidationError, validate_and_limit
from business_schema import POLICY

MAX_ROWS = 500


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM safe.orders",
    "SELECT o.id, oi.quantity FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id",
    "WITH t AS (SELECT id FROM safe.orders) SELECT * FROM t",
    "SELECT id FROM safe.orders UNION SELECT id FROM safe.refunds",
    "SELECT id FROM safe.orders LIMIT 10",
])
def test_allows_read_only_queries(sql: str) -> None:
    result = validate_and_limit(sql, MAX_ROWS, POLICY)
    assert result.sql


def test_adds_limit_when_missing() -> None:
    result = validate_and_limit("SELECT * FROM safe.orders", MAX_ROWS, POLICY)
    assert result.limit_applied is True
    assert "LIMIT 500" in result.sql.upper()


def test_keeps_smaller_limit() -> None:
    result = validate_and_limit("SELECT * FROM safe.orders LIMIT 10", MAX_ROWS, POLICY)
    assert result.limit_applied is False
    assert "LIMIT 10" in result.sql.upper()


def test_reduces_oversized_limit() -> None:
    result = validate_and_limit("SELECT * FROM safe.orders LIMIT 100000", MAX_ROWS, POLICY)
    assert result.limit_applied is True
    assert "LIMIT 500" in result.sql.upper()


@pytest.mark.parametrize("sql, reason", [
    ("SELECT 1; DROP TABLE safe.orders", "只允许查询语句"),
    ("DELETE FROM safe.orders", "只允许查询语句"),
    ("UPDATE safe.products SET price = 1", "只允许查询语句"),
    ("INSERT INTO safe.orders (id) VALUES (1)", "只允许查询语句"),
    ("TRUNCATE TABLE safe.orders", "只允许查询语句"),
    ("SELECT * FROM mysql.user", "系统库"),
    ("SELECT * FROM information_schema.tables", "系统库"),
    ("SELECT * FROM shop.orders", "未授权的库"),
    ("SELECT * FROM safe.unknown_table", "未授权的表"),
    ("SELECT SLEEP(10)", "危险函数"),
    ("SELECT BENCHMARK(1000, MD5('x'))", "危险函数"),
    ("SELECT * FROM safe.orders FOR UPDATE", "加锁查询"),
    ("SELECT * FROM safe.orders INTO OUTFILE '/tmp/x.csv'", "文件"),
])
def test_blocks_unsafe_queries(sql: str, reason: str) -> None:
    with pytest.raises(SQLValidationError) as error:
        validate_and_limit(sql, MAX_ROWS, POLICY)
    assert reason in str(error.value)


def test_rejects_empty_sql() -> None:
    with pytest.raises(SQLValidationError):
        validate_and_limit("   ", MAX_ROWS, POLICY)

def test_the_tables_come_from_the_policy() -> None:
    policy = SQLPolicy(schema="safe", dialect="mysql", columns={"orders": ["id"]})
    validate_and_limit("SELECT id FROM safe.orders", MAX_ROWS, policy)
    with pytest.raises(SQLValidationError, match="未授权的表：refunds"):
        validate_and_limit("SELECT id FROM safe.refunds", MAX_ROWS, policy)


def test_without_known_tables_only_the_schema_is_checked() -> None:
    policy = SQLPolicy(schema="safe", dialect="mysql")
    validate_and_limit("SELECT * FROM safe.anything", MAX_ROWS, policy)
    with pytest.raises(SQLValidationError, match="未授权的库"):
        validate_and_limit("SELECT * FROM shop.orders", MAX_ROWS, policy)
    with pytest.raises(SQLValidationError, match="系统库"):
        validate_and_limit("SELECT * FROM mysql.user", MAX_ROWS, policy)


def test_schema_and_dialect_come_from_the_policy() -> None:
    policy = SQLPolicy(schema="public", dialect="postgres", columns={"orders": ["id", "paid_at"]})
    result = validate_and_limit("SELECT id::text FROM public.orders", MAX_ROWS, policy)
    assert result.sql == "SELECT CAST(id AS TEXT) FROM public.orders LIMIT 500"
    with pytest.raises(SQLValidationError, match="未授权的库：safe"):
        validate_and_limit("SELECT id FROM safe.orders", MAX_ROWS, policy)
    with pytest.raises(SQLValidationError, match="字段不存在"):
        validate_and_limit("SELECT o.amount FROM public.orders o", MAX_ROWS, policy)
