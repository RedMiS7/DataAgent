from __future__ import annotations

import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from app import bootstrap, demo_data
from app.bootstrap import Bootstrapper, KeyValidationError, index_is_current, validate_keys
from app.config import Settings, apply_saved_keys, read_saved_keys, save_keys
from app.embedding.fake import HashEmbedder
from app.semantic import indexer
from app.vector.base import VectorHit


def make_settings(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(_env_file=None, local_env_file=tmp_path / ".env.local", **kwargs)


class StubRetriever:
    def __init__(self, embedder: Any = None, store: Any = None) -> None:
        self.embedder = embedder
        self.store = store


class StubEmbedder:
    model_name = "real-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


class StubStore:
    def __init__(self, count: int = 1, model: str = "real-model",
                 version: int = indexer.INDEX_VERSION) -> None:
        self.total = count
        self.model = model
        self.version = version

    def count(self, name: str) -> int:
        return self.total

    def search(self, name: str, vector: list[float], top_k: int) -> list[VectorHit]:
        return [VectorHit(id="a", score=1.0, payload={"model": self.model,
                                                      "index_version": self.version})]


def run_once(runner: Bootstrapper) -> None:
    """Run synchronously so tests need no thread timing."""
    runner._run_steps()


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Replaces every database touch and records what setup does."""
    calls: dict[str, list[Any]] = {"generate": [], "sync": [], "index": [], "tables": []}

    @contextmanager
    def no_connection(settings: Settings):
        yield None

    monkeypatch.setattr(bootstrap, "app_engine", lambda settings: None)
    monkeypatch.setattr(bootstrap, "ping", lambda engine: True)
    monkeypatch.setattr(bootstrap, "app_connection", no_connection)
    monkeypatch.setattr(bootstrap, "business_connection", no_connection)
    monkeypatch.setattr(bootstrap.session, "ensure_trace_table",
                        lambda conn: calls["tables"].append("call_traces"))
    monkeypatch.setattr(bootstrap.repository, "ensure_entity_column",
                        lambda conn: calls["tables"].append("is_entity"))
    monkeypatch.setattr(bootstrap.repository, "load_seed", lambda path: "seed")
    monkeypatch.setattr(bootstrap.repository, "sync_seed",
                        lambda conn, bundle: calls["sync"].append(bundle))
    monkeypatch.setattr(bootstrap.indexer, "build_index",
                        lambda *args: calls["index"].append(args))
    monkeypatch.setattr(demo_data, "has_orders", lambda settings: True)
    monkeypatch.setattr(demo_data, "generate",
                        lambda settings, progress: calls["generate"].append(settings))
    return calls


def step(runner: Bootstrapper, key: str) -> Any:
    return next(item for item in runner.status().steps if item.key == key)


def test_saved_keys_override_env_and_keep_the_other_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, llm_api_key="from-env")
    save_keys(settings, {"EMBEDDING_API_KEY": "embed"})
    save_keys(settings, {"LLM_API_KEY": "from-web"})
    assert settings.llm_api_key == "from-web"
    assert settings.embedding_api_key == "embed"

    fresh = make_settings(tmp_path, llm_api_key="from-env")
    apply_saved_keys(fresh)
    assert fresh.llm_api_key == "from-web"


def test_saved_key_file_is_private(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    save_keys(settings, {"LLM_API_KEY": "secret"})
    assert stat.S_IMODE(settings.local_env_file.stat().st_mode) == 0o600


def test_saved_file_ignores_unknown_entries(tmp_path: Path) -> None:
    path = tmp_path / ".env.local"
    path.write_text("LLM_API_KEY=abc=def\nMYSQL_HOST=evil\nEMBEDDING_API_KEY=\n", encoding="utf-8")
    assert read_saved_keys(path) == {"LLM_API_KEY": "abc=def"}


def test_existing_orders_are_never_regenerated(tmp_path: Path, offline: dict) -> None:
    runner = Bootstrapper(make_settings(tmp_path), StubRetriever())  # type: ignore[arg-type]
    run_once(runner)
    assert offline["generate"] == []
    assert step(runner, "demo_data").detail == "已有数据，保持不变"


def test_generate_refuses_a_database_with_orders(monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    monkeypatch.setattr(demo_data, "has_orders", lambda settings: True)
    with pytest.raises(RuntimeError, match="已有订单"):
        demo_data.generate(make_settings(tmp_path), lambda count: None)


def test_empty_database_gets_demo_data(tmp_path: Path, offline: dict,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo_data, "has_orders", lambda settings: False)
    runner = Bootstrapper(make_settings(tmp_path), StubRetriever())  # type: ignore[arg-type]
    run_once(runner)
    assert len(offline["generate"]) == 1
    assert runner.status().orders_total == demo_data.DEFAULT_ORDERS


def test_setup_waits_for_keys_then_finishes(tmp_path: Path, offline: dict) -> None:
    settings = make_settings(tmp_path)
    retriever = StubRetriever()
    runner = Bootstrapper(settings, retriever)  # type: ignore[arg-type]
    run_once(runner)
    status = runner.status()
    assert not status.ready and not status.llm_configured
    assert step(runner, "index").state == "waiting"

    settings.llm_api_key = "key"
    retriever.embedder = StubEmbedder()
    retriever.store = StubStore()
    run_once(runner)
    assert runner.status().ready
    assert offline["index"] == []


def test_stale_index_is_rebuilt(tmp_path: Path, offline: dict) -> None:
    retriever = StubRetriever(StubEmbedder(), StubStore(model="fake-bigram-v1"))
    runner = Bootstrapper(make_settings(tmp_path), retriever)  # type: ignore[arg-type]
    run_once(runner)
    assert len(offline["index"]) == 1


def test_index_is_current_checks_count_and_model(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    embedder = StubEmbedder()
    assert index_is_current(settings, StubStore(), embedder)  # type: ignore[arg-type]
    assert not index_is_current(settings, StubStore(count=0), embedder)  # type: ignore[arg-type]
    assert not index_is_current(settings, StubStore(model="other"), embedder)  # type: ignore[arg-type]
    # An index of an older version is rebuilt too.
    assert not index_is_current(settings, StubStore(version=1), embedder)  # type: ignore[arg-type]


def test_missing_vector_store_falls_back_to_keywords(tmp_path: Path, offline: dict,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "VECTOR_WAIT_SECONDS", 0)
    monkeypatch.setattr(bootstrap, "build_vector_store", lambda settings: None)
    settings = make_settings(tmp_path, llm_api_key="key")
    runner = Bootstrapper(settings, StubRetriever(StubEmbedder()))  # type: ignore[arg-type]
    run_once(runner)
    assert step(runner, "index").state == "skipped"
    assert runner.status().ready


def test_offline_embedder_skips_the_index(tmp_path: Path, offline: dict) -> None:
    retriever = StubRetriever(HashEmbedder(dim=8), StubStore())
    runner = Bootstrapper(make_settings(tmp_path), retriever)  # type: ignore[arg-type]
    run_once(runner)
    assert step(runner, "index").state == "skipped"


def test_failed_step_reports_the_error_and_can_retry(tmp_path: Path, offline: dict,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(path: Path) -> None:
        raise RuntimeError("种子文件损坏 secret-key")

    settings = make_settings(tmp_path, llm_api_key="secret-key")
    runner = Bootstrapper(settings, StubRetriever())  # type: ignore[arg-type]
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap.repository, "load_seed", broken)
        run_once(runner)
    status = runner.status()
    assert status.error == "种子文件损坏 ***"
    assert step(runner, "semantic").state == "failed"
    assert step(runner, "demo_data").state == "pending"

    run_once(runner)
    assert runner.status().error == ""


def test_bad_key_error_hides_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def rejected(model: Any) -> str:
        raise RuntimeError("401 invalid key sk-bad")

    monkeypatch.setattr(bootstrap, "probe_chat_model", rejected)
    with pytest.raises(KeyValidationError) as error:
        validate_keys(make_settings(tmp_path), "sk-bad", "")
    assert "sk-bad" not in str(error.value)
    assert "大模型 Key 验证失败" in str(error.value)


def test_setup_makes_sure_the_trace_table_exists(tmp_path: Path, offline: dict) -> None:
    runner = Bootstrapper(make_settings(tmp_path), StubRetriever())  # type: ignore[arg-type]
    runner._connect_database()
    # Both before the semantic layer is synced, which writes is_entity.
    assert offline["tables"] == ["call_traces", "is_entity"]
