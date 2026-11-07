from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

DEFAULT_EXPRESSION = "默认"


@dataclass(slots=True)
class TimeRange:
    start: date
    end: date
    label: str
    expression: str = ""
    # The period named after "比/相比/和…比" in the question, e.g. 上上个月 in "为什么比上上个月高".
    baseline: TimeRange | None = None

    @property
    def is_default(self) -> bool:
        """True when the text named no time and the 30-day default applied."""
        return self.expression == DEFAULT_EXPRESSION

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

_TIME_WORDS = (r"上上个月|上个月|上月|本月|这个月|去年|今年|本季度|这个季度|上周|上个星期"
               r"|今天|今日|昨天|昨日|(?:最近|近|过去)\s*\d{1,3}\s*(?:天|日|周|星期|个?月)")
# "比上上个月高" / "相比上个月" / "和去年相比". 占比 is a ratio, not a comparison.
_BASELINE = re.compile(
    rf"(?<!占)(?:比|相比于?|相较于?|较)\s*({_TIME_WORDS})"
    rf"|(?:和|跟|与|同)\s*({_TIME_WORDS})\s*(?:的\S{{0,8}}?)?\s*(?:相比|对比|比)"
)


def parse_time_range(question: str, as_of: date) -> TimeRange:
    """Parse the period the question asks about, and the period it is compared with.

    In "上个月比上上个月高多少", 上个月 is the period and 上上个月 the baseline. A
    question that names only a baseline ("为什么比上上个月高") gets the default
    period, so the caller can take the period from the conversation.
    """
    text = question.strip()
    match = _BASELINE.search(text)
    if match is None:
        return _parse_period(text, as_of)
    baseline = _parse_period(match.group(1) or match.group(2), as_of)
    current = _parse_period(f"{text[:match.start()]} {text[match.end():]}", as_of)
    current.baseline = baseline
    return current


def _months_spanned(value: TimeRange) -> int:
    """Whole months covered when the range starts and ends on the 1st, else 0."""
    if value.start.day != 1 or value.end.day != 1:
        return 0
    return (value.end.year - value.start.year) * 12 + value.end.month - value.start.month


def _span_label(start: date, end: date) -> str:
    return f"{start.isoformat()} 至 {(end - timedelta(days=1)).isoformat()}"


def previous_period(current: TimeRange) -> TimeRange:
    """The period right before, in the same unit: 9 月 -> 8 月 (all 31 days), not 30 days back.

    Month-aligned ranges move by whole months; others move by the same number of days.
    """
    end = current.start
    months = _months_spanned(current)
    if months:
        start = _add_months(end, -months)
    else:
        start = end - timedelta(days=max(1, (current.end - current.start).days))
    return TimeRange(start, end, _span_label(start, end), "上一周期")


def following_period(baseline: TimeRange, as_of: date) -> TimeRange | None:
    """The period right after the baseline, in the same unit, cut at the as-of date.

    Month-aligned baselines move by whole months (上个月 -> 本月至今, 去年 -> 今年至今);
    others move by the same number of days. None when nothing follows the baseline yet.
    """
    start = baseline.end
    months = _months_spanned(baseline)
    end = _add_months(start, months) if months else start + (baseline.end - baseline.start)
    end = min(end, as_of + timedelta(days=1))
    if end <= start:
        return None
    return TimeRange(start, end, _span_label(start, end), "对比推断", baseline)


def _parse_period(text: str, as_of: date) -> TimeRange:
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
    return TimeRange(start, end, "最近30天", DEFAULT_EXPRESSION)


def range_as_sql_params(value: TimeRange) -> dict[str, str]:
    return {"start": value.start.isoformat(), "end": value.end.isoformat()}