from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(slots=True)
class TimeRange:
    start: date
    end: date
    label: str
    expression: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end_exclusive": self.end.isoformat(),
            "label": self.label,
        }


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    return date(month_index // 12, month_index % 12 + 1, 1)


_N_DAYS = re.compile(r"(?:最近|近|过去)\s*(\d{1,3})\s*(?:天|日)")
_WEEKS = re.compile(r"(?:最近|近|过去)\s*(\d{1,2})\s*(?:周|星期)")
_MONTHS = re.compile(r"(?:最近|近|过去)\s*(\d{1,2})\s*个?月")


def parse_time_range(question: str, as_of: date) -> TimeRange:
    text = question.strip()
    end = as_of + timedelta(days=1)

    if "上上个月" in text:
        start = _add_months(_month_start(as_of), -2)
        return TimeRange(start, _add_months(start, 1), "上上个月", "上上个月")
    if "上个月" in text or "上月" in text:
        start = _add_months(_month_start(as_of), -1)
        return TimeRange(start, _add_months(start, 1), "上个月", "上个月")
    if "本月" in text or "这个月" in text:
        return TimeRange(_month_start(as_of), end, "本月至今", "本月")
    if "去年" in text:
        start = date(as_of.year - 1, 1, 1)
        return TimeRange(start, date(as_of.year, 1, 1), "去年", "去年")
    if "今年" in text:
        return TimeRange(date(as_of.year, 1, 1), end, "今年至今", "今年")
    if "本季度" in text or "这个季度" in text:
        quarter = (as_of.month - 1) // 3
        start = date(as_of.year, quarter * 3 + 1, 1)
        return TimeRange(start, end, "本季度至今", "本季度")
    if "上个月" not in text and ("上周" in text or "上个星期" in text):
        start = as_of - timedelta(days=as_of.weekday() + 7)
        return TimeRange(start, start + timedelta(days=7), "上周", "上周")
    if "今天" in text or "今日" in text:
        return TimeRange(as_of, end, "今天", "今天")
    if "昨天" in text or "昨日" in text:
        start = as_of - timedelta(days=1)
        return TimeRange(start, as_of, "昨天", "昨天")

    match = _N_DAYS.search(text)
    if match:
        days = max(1, int(match.group(1)))
        start = as_of - timedelta(days=days - 1)
        return TimeRange(start, end, f"最近{days}天", match.group(0))

    match = _WEEKS.search(text)
    if match:
        weeks = max(1, int(match.group(1)))
        days = weeks * 7
        start = as_of - timedelta(days=days - 1)
        return TimeRange(start, end, f"最近{days}天", match.group(0))

    match = _MONTHS.search(text)
    if match:
        months = max(1, int(match.group(1)))
        start = _add_months(_month_start(as_of), -(months - 1))
        return TimeRange(start, end, f"最近{months}个月", match.group(0))

    start = as_of - timedelta(days=29)
    return TimeRange(start, end, "最近30天", "默认")


def range_as_sql_params(value: TimeRange) -> dict[str, str]:
    return {"start": value.start.isoformat(), "end": value.end.isoformat()}