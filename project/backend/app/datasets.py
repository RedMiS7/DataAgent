"""Runtime metadata shared by the application and the evaluation CLI."""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SuggestedQuestion(BaseModel):
    tag: NonEmptyText
    text: NonEmptyText
    mode: Literal["query", "analysis"] = "query"


class EvaluationFiles(BaseModel):
    questions: Path
    reviews: Path | None = None
    routing: Path | None = None
    security: Path | None = None


class DatasetManifest(BaseModel):
    # Import manifests also describe raw files, columns and database grants.
    model_config = ConfigDict(extra="ignore")

    id: NonEmptyText
    database: NonEmptyText
    as_of_date: date | None = None
    suggested_questions: list[SuggestedQuestion] = Field(default_factory=list)
    sensitive_terms: list[NonEmptyText] = Field(default_factory=list)
    eval: EvaluationFiles | None = None


class DatasetManifestError(RuntimeError):
    pass


def load_dataset_manifest(path: Path) -> DatasetManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("清单必须是 JSON 对象")
        # Existing import manifests can use the directory name as their dataset ID.
        payload.setdefault("id", path.parent.name)
        manifest = DatasetManifest.model_validate(payload)
    except (OSError, ValueError) as exc:
        raise DatasetManifestError(f"读取数据集清单 {path} 失败：{exc}") from exc
    if manifest.eval is not None:
        files = manifest.eval
        for name in ("questions", "reviews", "routing", "security"):
            value = getattr(files, name)
            if value is not None:
                setattr(files, name, (path.parent / value).resolve())
        suffix = files.questions.suffix.lower()
        if suffix not in (".json", ".csv") or (suffix == ".csv" and files.reviews is None):
            raise DatasetManifestError(f"数据集清单 {path} 需要 JSON 题集或带 reviews 的 CSV 题集")
    return manifest


@lru_cache(maxsize=8)
def dataset_manifests(data_dir: Path) -> dict[str, DatasetManifest]:
    """Discover manifests by their business database, with a stable ID for stored runs."""
    by_database: dict[str, DatasetManifest] = {}
    ids: set[str] = set()
    for path in sorted(data_dir.glob("*/manifest.json")):
        manifest = load_dataset_manifest(path)
        if manifest.database in by_database or manifest.id in ids:
            raise DatasetManifestError(f"数据集清单 {path} 的业务库或数据集 ID 重复")
        by_database[manifest.database] = manifest
        ids.add(manifest.id)
    return by_database
