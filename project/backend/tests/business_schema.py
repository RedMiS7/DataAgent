"""The business views as the read-only account sees them, read from the init SQL."""

from __future__ import annotations

import re
from pathlib import Path

from app.safety.validator import SQLPolicy

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "data" / "initdb" / "01_business_schema.sql"


def view_columns() -> dict[str, list[str]]:
    """The columns of the safe views."""
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    views = re.findall(r"VIEW safe\.(\w+) AS\s*SELECT (.*?) FROM", text, re.S)
    return {table: [part.strip() for part in body.replace("\n", " ").split(",")]
            for table, body in views}


COLUMNS = view_columns()
POLICY = SQLPolicy(schema="safe", dialect="mysql", columns=COLUMNS)
