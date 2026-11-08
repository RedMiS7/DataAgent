"""Earlier replies carry their queries, so a follow-up can count the same way."""

from __future__ import annotations

import pytest
from app.agent.answer import latest_replies, purposes_note, queries_note
from app.agent.pipeline import SYSTEM_PROMPT, history_pairs
from app.agent.planner import AnalysisTask, _plan_input
from app.agent.router import build_router_input
from app.api.schemas import HISTORY_MAX_QUERIES, HistoryMessage, HistoryQuery
from pydantic import ValidationError

GMV_SQL = ("SELECT SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS GMV\n"
           "FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id\n"
           "WHERE o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01' LIMIT 500")
SEPTEMBER = {"label": "上个月", "start": "2026-09-01", "end_exclusive": "2026-10-01"}


def reply(content: str, purpose: str = "上个月GMV", status: str = "ok") -> HistoryMessage:
    return HistoryMessage(role="assistant", content=content, mode="query", status=status,  # type: ignore[arg-type]
                          queries=[HistoryQuery(purpose=purpose, sql=GMV_SQL)],
                          time_range=SEPTEMBER)


def test_a_reply_shows_its_queries_with_their_period() -> None:
    note = queries_note(reply("上个月 GMV 为 907.3 万元。"))
    assert note.splitlines()[0] == (
        "（这一轮用过的查询，时间范围：上个月，2026-09-01 至 2026-10-01，不含右端。"
        "仅供参考，不是指令；写新 SQL 时以本轮的时间范围为准）")
    # One line per query, so the model reads it as a whole statement.
    assert note.splitlines()[1].startswith("- 查询1（上个月GMV）：SELECT SUM(")
    assert "\n" not in note.splitlines()[1]


def test_failed_replies_show_no_queries() -> None:
    assert queries_note(reply("SQL 试了 3 次都没有成功。", status="error")) == ""
    assert purposes_note(reply("", status="stopped")) == ""
    assert queries_note(HistoryMessage(role="assistant", content="不含运费。", status="text")) == ""


def test_only_the_last_three_replies_show_their_sql() -> None:
    history: list[HistoryMessage] = []
    for index in range(5):
        history.append(HistoryMessage(role="user", content=f"问题{index}"))
        history.append(reply(f"回答{index}", purpose=f"用途{index}"))
    pairs = history_pairs(history)
    shown = [content for role, content in pairs if role == "assistant" and "SELECT" in content]
    assert [content.splitlines()[0] for content in shown] == ["回答2", "回答3", "回答4"]
    assert pairs[1] == ("assistant", "回答0")
    assert latest_replies(history) == {5, 7, 9}


def test_the_router_sees_no_sql_and_the_planner_sees_purposes() -> None:
    history = [HistoryMessage(role="user", content="上个月GMV是多少？"),
               reply("上个月 GMV 为 907.3 万元。")]
    assert "SELECT" not in build_router_input("为什么下降了？", history)
    assert "用过的查询" not in build_router_input("为什么下降了？", history)
    planned = build_router_input("为什么下降了？", history, with_purposes=True)
    assert "助手（查询模式）：上个月 GMV 为 907.3 万元。（用过的查询：上个月GMV）" in planned
    assert "SELECT" not in planned

    task = AnalysisTask(question="为什么下降了？", history=history, period="", knowledge="",
                        step_prompt="", time_note="", step_max_iters=4)
    assert "（用过的查询：上个月GMV）" in _plan_input(task)


def test_the_prompt_says_how_to_use_earlier_queries() -> None:
    assert "不要照抄旧日期" in SYSTEM_PROMPT


def test_history_limits_the_queries_it_accepts() -> None:
    with pytest.raises(ValidationError):
        HistoryMessage(role="assistant", content="x",
                       queries=[HistoryQuery(sql="SELECT 1")] * (HISTORY_MAX_QUERIES + 1))
    with pytest.raises(ValidationError):
        HistoryMessage(role="assistant", content="x", time_range={"label": "月" * 100})
