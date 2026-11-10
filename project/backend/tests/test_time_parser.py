from __future__ import annotations

from datetime import date

import pytest
from app.nl2sql.time_parser import following_period, parse_time_range, previous_period

AS_OF = date(2026, 10, 31)


@pytest.mark.parametrize("question, label, start, end", [
    ("上个月哪个商品退货率最高", "上个月", "2026-09-01", "2026-10-01"),
    ("那上上个月呢", "上上个月", "2026-08-01", "2026-09-01"),
    ("最近7天订单数", "最近7天", "2026-10-25", "2026-11-01"),
    ("最近30天GMV趋势", "最近30天", "2026-10-02", "2026-11-01"),
    ("今年的GMV", "今年至今", "2026-01-01", "2026-11-01"),
    ("今天卖了多少", "今天", "2026-10-31", "2026-11-01"),
    ("昨天卖了多少", "昨天", "2026-10-30", "2026-10-31"),
    ("本月订单数", "本月至今", "2026-10-01", "2026-11-01"),
    ("2024 年 12 月的销售额", "2024-12-01 至 2024-12-31", "2024-12-01", "2025-01-01"),
    ("2024-02", "2024-02-01 至 2024-02-29", "2024-02-01", "2024-03-01"),
    ("2024年", "2024-01-01 至 2024-12-31", "2024-01-01", "2025-01-01"),
    ("2024年2月29日", "2024-02-29 至 2024-02-29", "2024-02-29", "2024-03-01"),
    ("2024-02-01 至 2024-02-29", "2024-02-01 至 2024-02-29", "2024-02-01", "2024-03-01"),
    ("2024-02-01 - 2024-02-29", "2024-02-01 至 2024-02-29", "2024-02-01", "2024-03-01"),
    ("2024年11月到2025年2月", "2024-11-01 至 2025-02-28", "2024-11-01", "2025-03-01"),
])
def test_parse_time_range(question: str, label: str, start: str, end: str) -> None:
    result = parse_time_range(question, AS_OF)
    assert result.label == label
    assert result.start.isoformat() == start
    assert result.end.isoformat() == end


def test_default_range_is_last_30_days() -> None:
    result = parse_time_range("卖得怎么样", AS_OF)
    assert result.label == "最近30天"
    assert result.start.isoformat() == "2026-10-02"


@pytest.mark.parametrize("text", ["2025-02-29", "2024年13月", "2024-02-29 至 2024-02-01"])
def test_invalid_absolute_time_is_unparsed(text: str) -> None:
    assert parse_time_range(text, AS_OF).is_default


def test_as_dict_shape() -> None:
    payload = parse_time_range("上个月", AS_OF).as_dict()
    assert payload == {"start": "2026-09-01", "end_exclusive": "2026-10-01",
                       "label": "上个月"}

@pytest.mark.parametrize("question, label, baseline, baseline_start", [
    ("上个月GMV比上上个月高多少", "上个月", "上上个月", "2026-08-01"),
    ("本月为什么比上个月低", "本月至今", "上个月", "2026-09-01"),
    ("今年和去年相比怎么样", "今年至今", "去年", "2025-01-01"),
    ("最近7天订单数较上周多吗", "最近7天", "上周", "2026-10-19"),
    ("这个月相比于上个月怎么样", "本月至今", "上个月", "2026-09-01"),
    ("2025年2月比2024年2月高多少", "2025-02-01 至 2025-02-28",
     "2024-02-01 至 2024-02-29", "2024-02-01"),
    ("2025年2月和2024年2月相比怎么样", "2025-02-01 至 2025-02-28",
     "2024-02-01 至 2024-02-29", "2024-02-01"),
    ("本月与2025年2月相比怎么样", "本月至今", "2025-02-01 至 2025-02-28", "2025-02-01"),
])
def test_comparison_names_the_period_and_its_baseline(question: str, label: str, baseline: str,
                                                      baseline_start: str) -> None:
    result = parse_time_range(question, AS_OF)
    assert result.label == label
    assert result.baseline is not None
    assert result.baseline.label == baseline
    assert result.baseline.start.isoformat() == baseline_start


def test_a_question_naming_only_the_baseline_keeps_the_default_period() -> None:
    result = parse_time_range("为什么比上上个月高？", AS_OF)
    assert result.is_default
    assert result.baseline is not None
    assert (result.baseline.start.isoformat(), result.baseline.end.isoformat()) == (
        "2026-08-01", "2026-09-01")


@pytest.mark.parametrize("question", ["上个月GMV是多少", "各渠道GMV占比上个月是多少"])
def test_no_comparison_means_no_baseline(question: str) -> None:
    result = parse_time_range(question, AS_OF)
    assert result.label == "上个月"
    assert result.baseline is None


@pytest.mark.parametrize("baseline, start, end", [
    ("上上个月", "2026-09-01", "2026-10-01"),
    ("上个月", "2026-10-01", "2026-11-01"),
    ("去年", "2026-01-01", "2026-11-01"),
    ("上周", "2026-10-26", "2026-11-01"),
])
def test_following_period(baseline: str, start: str, end: str) -> None:
    base = parse_time_range(baseline, AS_OF)
    result = following_period(base, AS_OF)
    assert result is not None
    assert (result.start.isoformat(), result.end.isoformat()) == (start, end)
    assert result.baseline is base
    assert not result.is_default


def test_nothing_follows_a_period_that_runs_to_the_as_of_date() -> None:
    assert following_period(parse_time_range("本月", AS_OF), AS_OF) is None


@pytest.mark.parametrize("question, start, end", [
    ("上个月", "2026-08-01", "2026-09-01"),
    ("上上个月", "2026-07-01", "2026-08-01"),
    ("本月", "2026-09-01", "2026-10-01"),
    ("去年", "2024-01-01", "2025-01-01"),
    ("最近7天", "2026-10-18", "2026-10-25"),
    ("最近30天", "2026-09-02", "2026-10-02"),
    ("上周", "2026-10-12", "2026-10-19"),
])
def test_previous_period(question: str, start: str, end: str) -> None:
    result = previous_period(parse_time_range(question, AS_OF))
    assert (result.start.isoformat(), result.end.isoformat()) == (start, end)


def test_previous_period_label_names_the_last_day() -> None:
    assert previous_period(parse_time_range("上个月", AS_OF)).label == "2026-08-01 至 2026-08-31"
