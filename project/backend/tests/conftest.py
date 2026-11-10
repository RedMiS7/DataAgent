from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_column_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests run without a database: run_sql gets no columns and skips the column check,
    and the prompt keeps the retrieved columns. The intent is checked against an empty
    semantic layer, and the router names only a route, so no intent is read. Tests that
    need columns, a semantic layer, an intent or a rewrite set them."""
    from agentscope.message import TextBlock
    from app.agent import pipeline, tools
    from app.agent.intent import Catalog
    from app.agent.scope_model import ScriptedChatModel

    monkeypatch.setattr(tools, "known_columns", lambda settings: None)
    monkeypatch.setattr(tools, "table_columns", lambda settings: None)
    monkeypatch.setattr(pipeline, "load_catalog", lambda settings, conn: Catalog())
    monkeypatch.setattr(pipeline, "build_router_model", lambda settings: ScriptedChatModel(
        [[TextBlock(text='{"route": "query", "reason": "测试"}')]]))
    # A conclusion that is no JSON is rewritten once; the rewrite gives nothing, so the
    # model's text is shown as it is.
    monkeypatch.setattr(pipeline, "build_rewrite_model",
                        lambda settings: ScriptedChatModel([[TextBlock(text="")]]))
