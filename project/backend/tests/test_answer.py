from __future__ import annotations

from app.agent.answer import (
    extract_sql,
    recommend_chart,
    template_summary,
    verify_summary,
)


def test_extract_sql_from_markdown() -> None:
    text = "```sql\nSELECT 1 FROM safe.orders;\n```"
    assert extract_sql(text) == "SELECT 1 FROM safe.orders"


def test_extract_sql_from_chatter() -> None:
    text = "好的，查询如下：SELECT COUNT(*) FROM safe.orders"
    assert extract_sql(text).startswith("SELECT COUNT(*)")


def test_chart_for_time_series() -> None:
    chart = recommend_chart(["stat_date", "gmv"], [["2026-10-01", 1], ["2026-10-02", 2]])
    assert chart.type == "line"


def test_chart_for_small_category() -> None:
    chart = recommend_chart(["channel", "gmv"], [["a", 1], ["b", 2]])
    assert chart.type == "pie"


def test_chart_for_large_category() -> None:
    rows = [[f"channel-{index}", index] for index in range(8)]
    chart = recommend_chart(["channel", "gmv"], rows)
    assert chart.type == "bar"


def test_chart_for_single_row() -> None:
    assert recommend_chart(["gmv"], [[100]]).type == "table"


def test_summary_with_known_number_passes() -> None:
    rows = [[100.0, "A"], [50.0, "B"]]
    assert verify_summary("A 的值为 100。", rows) is True


def test_summary_with_unknown_number_fails() -> None:
    rows = [[100.0, "A"], [50.0, "B"]]
    assert verify_summary("A 的值为 12345。", rows) is False


def test_summary_rejecting_sql_text() -> None:
    rows = [[1]]
    assert verify_summary("SELECT 1 AS result。", rows) is False


def test_summary_rejecting_empty_text() -> None:
    assert verify_summary("", [[1]]) is False


def test_template_summary_is_stable() -> None:
    text = template_summary(["name", "refund_amount"], [["A", 10.0], ["B", 5.0]])
    assert "共 2 行" in text
    assert "A" in text