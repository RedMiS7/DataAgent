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
    and the prompt keeps the retrieved columns. Tests that need columns set them."""
    from app.agent import tools

    monkeypatch.setattr(tools, "known_columns", lambda settings: None)
    monkeypatch.setattr(tools, "table_columns", lambda settings: None)
