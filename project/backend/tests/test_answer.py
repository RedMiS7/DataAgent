from __future__ import annotations

from app.agent.answer import (
    extract_sql,
    recommend_chart,
    template_summary,
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


def test_chart_finds_dates_and_measures_independent_of_column_order() -> None:
    columns = ["reference_id", "label", "value", "period"]
    rows = [[101, "甲", 4, "2026-09"], [102, "乙", 5, "2026-10"]]
    chart = recommend_chart(columns, rows)
    assert (chart.type, chart.x_field, chart.y_fields) == ("line", "period", ["value"])


def test_chart_uses_values_instead_of_time_aliases_and_handles_missing_measures() -> None:
    columns = ["account_id", "date", "value"]
    rows = [[101, "2026-13", 2], [102, "2026-14", None], [103, "2026-15", -5]]
    chart = recommend_chart(columns, rows)
    assert (chart.type, chart.x_field, chart.y_fields) == ("bar", "date", ["value"])


def test_chart_falls_back_when_there_is_no_distinct_label_or_number() -> None:
    assert recommend_chart(["x", "y"], [["same", 1], ["same", 2]]).type == "table"
    assert recommend_chart(["x", "y"], [["A", True], ["B", False]]).type == "table"
    assert recommend_chart(["x", "y_id"], [["A", 1], ["B", 2]]).type == "table"


def test_template_summary_is_stable() -> None:
    text = template_summary(["name", "refund_amount"], [["A", 10.0], ["B", 5.0]])
    assert "共 2 行" in text
    assert "A" in text
