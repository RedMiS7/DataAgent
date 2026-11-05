from __future__ import annotations

from datetime import date

import pytest
from app.nl2sql.time_parser import parse_time_range

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


def test_as_dict_shape() -> None:
    payload = parse_time_range("上个月", AS_OF).as_dict()
    assert payload == {"start": "2026-09-01", "end_exclusive": "2026-10-01",
                       "label": "上个月"}