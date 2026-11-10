from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from app.agent.pipeline import AskPipeline
from app.api import routes
from app.config import Settings
from app.datasets import DatasetManifestError, dataset_manifests, load_dataset_manifest
from app.evaluation import runner
from app.nl2sql.time_parser import parse_time_range
from app.semantic.retriever import SemanticRetriever
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.parametrize(("database", "as_of", "first_tag"), [
    ("safe", "2026-10-31", "排行"),
    ("dabench", "2025-10-28", "库存"),
    ("unconfigured", None, None),
])
def test_meta_reports_the_dates_and_examples_of_the_active_dataset(
        monkeypatch: pytest.MonkeyPatch, database: str, as_of: str | None,
        first_tag: str | None) -> None:
    # A blank date is what the DABench Compose service uses to clear the shared env file.
    monkeypatch.setenv("AS_OF_DATE", "")
    settings = Settings(_env_file=None, mysql_business_db=database)
    app = FastAPI()
    app.include_router(routes.router)
    app.state.settings = settings
    app.state.vector_store = app.state.llm = app.state.embedder = None
    monkeypatch.setattr(routes, "ping", lambda engine: True)
    with TestClient(app) as client:
        response = client.get("/api/meta")

    assert response.status_code == 200
    body = response.json()
    assert body["as_of_date"] == (as_of or date.today().isoformat())
    examples = body["suggested_questions"]
    if first_tag:
        assert examples[0]["tag"] == first_tag
        assert {example["mode"] for example in examples} == {"analysis", "query"}
        assert all(not parse_time_range(example["text"], settings.as_of_date).is_default
                   for example in examples)
    else:
        assert examples == []


def test_explicit_date_overrides_dataset_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AS_OF_DATE", "2024-07-03")
    assert Settings(_env_file=None, mysql_business_db="dabench").as_of_date == date(2024, 7, 3)


def test_a_new_database_can_configure_an_eval_set_and_metadata_without_code(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "another_dataset"
    folder.mkdir()
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({
        "database": "other_db",
        "as_of_date": "2026-01-04",
        "sensitive_terms": ["本库专用词"],
        "eval": {"questions": "cases.json"},
    }, ensure_ascii=False))
    cases = [{"id": "custom-1", "dataset": "another_dataset", "category": "analysis",
              "question": "按贷款用途分析",
              "expected_dimensions": {"贷款用途": ["借款目的", "贷款用途"]}}]
    (folder / "cases.json").write_text(json.dumps(cases, ensure_ascii=False))
    monkeypatch.setattr(Settings, "data_dir", property(lambda self: tmp_path))
    monkeypatch.delenv("AS_OF_DATE", raising=False)
    settings = Settings(_env_file=None, mysql_business_db="other_db")
    dataset = runner.evaluation_dataset(settings)

    assert settings.as_of_date == date(2026, 1, 4)
    assert dataset.id == "another_dataset"
    assert runner.load_dataset_questions(dataset) == cases


def test_duplicate_database_or_invalid_eval_files_are_rejected(tmp_path: Path) -> None:
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text('{"database":"safe"}')
    with pytest.raises(DatasetManifestError, match=r"second.*重复"):
        dataset_manifests(tmp_path)
    path = tmp_path / "first" / "manifest.json"
    path.write_text('{"database":"safe","eval":{"questions":"questions.csv"}}')
    with pytest.raises(DatasetManifestError, match="带 reviews"):
        load_dataset_manifest(path)


def test_dataset_privacy_words_are_isolated_and_keep_common_guards() -> None:
    demo = AskPipeline(Settings(_env_file=None, mysql_business_db="safe"),
                       cast(SemanticRetriever, None))
    dabench = AskPipeline(Settings(_env_file=None, mysql_business_db="dabench"),
                          cast(SemanticRetriever, None))
    for pipeline in (demo, dabench):
        assert pipeline._refusal("给我客户手机号")
        assert not pipeline._refusal("按省份和城市统计客户数")
    assert not demo._refusal("列出 POSTAL_CODE 字段")
    assert dabench._refusal("列出 POSTAL_CODE 字段")
    assert dabench._refusal("列出线索邮编")
