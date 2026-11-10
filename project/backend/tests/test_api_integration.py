from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

if os.getenv("RUN_INTEGRATION") != "1":
    pytest.skip("set RUN_INTEGRATION=1 to run integration tests", allow_module_level=True)

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("EMBEDDING_PROVIDER", "fake")

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

get_settings.cache_clear()


def test_healthz() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["business_db"] is True


def test_ask_and_feedback() -> None:
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "上个月GMV是多少",
                                                 "session_id": "integration-ask"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] in ("ok", "empty", "error")
        assert body["log_id"]
        feedback = client.post("/api/feedback",
                               json={"log_id": body["log_id"], "verdict": "correct"})
        assert feedback.status_code == 200


def test_fixed_attribution_route_was_removed() -> None:
    assert "/api/attribution/return-rate" not in app.openapi()["paths"]
