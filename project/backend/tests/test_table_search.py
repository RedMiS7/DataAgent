"""Table search: hybrid retrieval, rerank, completion; and the rerank adapter."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.rerank import RerankError, RerankResult, build_reranker
from app.rerank.dashscope import DashScopeReranker
from app.rerank.fake import FakeReranker
from app.semantic import repository, table_search
from app.semantic.models import CaseHit, Metric, SchemaItem, TableSemantic
from app.semantic.repository import ForeignKey
from app.semantic.retriever import SemanticRetriever
from app.semantic.schema_context import hint_keys, join_graph, pointed_tables
from app.semantic.table_search import TableSearcher, fuse
from app.vector.base import VectorHit, VectorStoreError
from sqlalchemy import create_engine, text

SEED = Path(__file__).resolve().parents[2] / "data" / "seed"
ITEMS = [SchemaItem.model_validate(row)
         for row in json.loads((SEED / "schema_items.json").read_text("utf-8"))]
TABLES = [TableSemantic.model_validate(row)
          for row in json.loads((SEED / "semantic_tables.json").read_text("utf-8"))]
METRICS = [Metric.model_validate(row)
           for row in json.loads((SEED / "metrics.json").read_text("utf-8"))]


class StubEmbedder:
    model_name = "stub-embedding"
    dim = 1

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]


class StubStore:
    """Returns the hits given for each collection, by name suffix."""

    def __init__(self, hits: dict[str, list[tuple[dict, float]]], fail: bool = False) -> None:
        self.hits = hits
        self.fail = fail

    def search(self, name: str, vector: list[float], top_k: int,
               filters: dict | None = None) -> list[VectorHit]:
        if self.fail:
            raise VectorStoreError("qdrant down")
        for suffix, hits in self.hits.items():
            if name.endswith(suffix):
                return [VectorHit(id=str(i), score=score, payload=payload)
                        for i, (payload, score) in enumerate(hits)][:top_k]
        return []


class OrderReranker:
    """Puts the documents in a fixed order of table names."""

    model_name = "order-rerank"

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls: list[tuple[str, int]] = []

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
        self.calls.append((query, len(documents)))
        ranked = [index for name in self.order for index, document in enumerate(documents)
                  if document.startswith(f"safe.{name}：")]
        return [RerankResult(index=index, score=1.0 - rank / 10)
                for rank, index in enumerate(ranked[:top_n])]


class FailingReranker:
    model_name = "failing"

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
        raise RerankError("重排模型返回 HTTP 401：InvalidApiKey")


ENTITIES = [{"entity_type": "channels.name", "value": "直播带货",
             "table_name": "safe.channels", "column_name": "name"}]


@pytest.fixture
def semantic(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    monkeypatch.setattr(repository, "fetch_tables", lambda conn: TABLES)
    monkeypatch.setattr(repository, "fetch_schema_items",
                        lambda conn, enabled_only=True: [item for item in ITEMS
                                                         if item.enabled or not enabled_only])
    monkeypatch.setattr(repository, "fetch_metrics", lambda conn: METRICS)
    monkeypatch.setattr(repository, "fetch_cases",
                        lambda conn: [CaseHit(id=7, question="上个月退款最多的商品",
                                              sql_text="SELECT p.name FROM safe.refunds r JOIN "
                                                       "safe.products p ON p.id = r.product_id")])
    monkeypatch.setattr(repository, "fetch_entity_values", lambda conn, items: ENTITIES)
    monkeypatch.setattr(repository, "fetch_foreign_keys", lambda conn: [])
    monkeypatch.setattr(table_search, "business_connection", no_connection)


def _column(table: str, column: str) -> dict:
    return {"table": f"safe.{table}", "column": column}


CHANNEL_HITS = {
    "schema_items": [(_column("orders", "channel_id"), 0.62),
                     (_column("ad_daily_stats", "attributed_orders"), 0.61),
                     (_column("orders", "total_amount"), 0.55)],
    "metrics": [({"name": "订单数"}, 0.61)],
}


def _searcher(hits: dict | None = None, reranker: Any = None, fail: bool = False,
              **settings: Any) -> TableSearcher:
    config = Settings(_env_file=None, **settings)
    store = StubStore(hits if hits is not None else CHANNEL_HITS, fail=fail)
    return TableSearcher(config, SemanticRetriever(config, store, StubEmbedder()),  # type: ignore[arg-type]
                         reranker)


def test_fuse_adds_reciprocal_ranks() -> None:
    fused = fuse([["a", "b", "a"], ["b", "c"]])
    assert [key for key, _ in fused] == ["b", "a", "c"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_hybrid_ranks_tables_by_their_best_column(semantic: None) -> None:
    search = _searcher().search("最近30天各渠道的订单数是多少？", None, use_keyword=False,  # type: ignore[arg-type]
                                rerank=False, complete=False)
    assert search.table_names[:2] == ["orders", "ad_daily_stats"]
    assert search.tables[0].reasons == ["检索命中"]
    assert not search.reranked and search.rerank_note == ""


def test_completion_adds_the_table_a_matched_column_links_to(semantic: None) -> None:
    search = _searcher().search("最近30天各渠道的订单数是多少？", None, use_keyword=False,  # type: ignore[arg-type]
                                rerank=False)
    reasons = {choice.table: choice.reasons for choice in search.tables}
    # orders.channel_id is linked to channels.id.
    assert reasons["channels"] == ["外键指向"]


def test_completion_adds_metric_entity_and_joining_tables(semantic: None) -> None:
    hits = {"schema_items": [(_column("categories", "name"), 0.6)],
            "metrics": [({"name": "退货率"}, 0.7)]}
    search = _searcher(hits, search_top_tables=1).search(
        "直播带货渠道各类目的退货率", None, use_keyword=False, rerank=False)  # type: ignore[arg-type]
    reasons = {choice.table: choice.reasons[0] for choice in search.tables}
    assert reasons["categories"] == "检索命中"
    assert reasons["refunds"] == reasons["orders"] == reasons["order_items"] == "口径需要"
    assert reasons["channels"] == "实体所在"
    # categories reaches the orders through products.
    assert reasons["products"] == "关联补全"
    assert search.entities[0].table_name == "safe.channels"


def test_only_named_values_add_their_table(semantic: None) -> None:
    hits = {"schema_items": [(_column("orders", "paid_at"), 0.6)],
            "entity_values": [({"entity_type": "products.name", "value": "鞋靴热销款232号",
                                "table_name": "safe.products", "column_name": "name"}, 0.5)]}
    search = _searcher(hits, search_top_tables=1).search(
        "上个月的订单数", None, use_keyword=False, rerank=False)  # type: ignore[arg-type]
    assert [entity.value for entity in search.entities] == ["鞋靴热销款232号"]
    assert "products" not in search.table_names


def test_the_rerank_model_picks_the_tables(semantic: None) -> None:
    reranker = OrderReranker(["channels", "orders"])
    hits = {"schema_items": [(_column("orders", "channel_id"), 0.6),
                             (_column("channels", "name"), 0.5),
                             (_column("ad_daily_stats", "spend"), 0.4)]}
    search = _searcher(hits, reranker, search_top_tables=2).search(
        "各渠道的订单数", None, use_keyword=False, complete=False)  # type: ignore[arg-type]
    assert search.table_names == ["channels", "orders"]
    assert search.reranked and search.tables[0].reasons == ["重排选中"]
    assert search.tables[0].score == pytest.approx(1.0)
    assert reranker.calls == [("各渠道的订单数", 3)]
    assert search.candidates == ["orders", "channels", "ad_daily_stats"]


def test_only_the_best_candidates_go_to_the_rerank_model(semantic: None) -> None:
    reranker = OrderReranker([])
    _searcher(reranker=reranker, rerank_candidates=1).search(
        "各渠道的订单数", None, use_keyword=False)  # type: ignore[arg-type]
    assert reranker.calls[0][1] == 1


def test_a_failing_rerank_keeps_the_retrieval_order(semantic: None,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    search = _searcher(reranker=FailingReranker()).search(
        "各渠道的订单数", None, use_keyword=False, complete=False)  # type: ignore[arg-type]
    assert search.table_names[:2] == ["orders", "ad_daily_stats"]
    assert not search.reranked
    assert search.rerank_note == "重排不可用：重排模型返回 HTTP 401：InvalidApiKey"
    assert "重排失败" in caplog.text


def test_without_a_rerank_model_the_note_says_so(semantic: None) -> None:
    search = _searcher().search("各渠道的订单数", None, use_keyword=False, complete=False)  # type: ignore[arg-type]
    assert search.rerank_note == "没有配置重排模型，按检索排序"


def test_without_vectors_keywords_still_find_tables(semantic: None) -> None:
    search = _searcher(fail=True).search("上个月各承运商发了多少包裹", None,  # type: ignore[arg-type]
                                         rerank=False, complete=False)
    assert search.degraded.startswith("向量检索不可用：qdrant down")
    assert "shipments" in search.table_names


def test_cases_point_to_the_tables_of_their_sql(semantic: None) -> None:
    hits = {"qa_cases": [({"case_id": 7}, 0.9)]}
    search = _searcher(hits).search("退款最多的商品", None, use_keyword=False,  # type: ignore[arg-type]
                                    rerank=False, complete=False)
    assert search.table_names == ["refunds", "products"]
    assert [case.id for case in search.cases] == [7]


# Links between tables

def test_hint_keys_and_pointed_tables() -> None:
    keys = hint_keys(ITEMS, "safe.")
    assert ForeignKey("orders", "channel_id", "channels", "id") in keys
    assert pointed_tables([("orders", "channel_id"), ("refunds", "product_id"),
                           ("orders", "paid_at")], keys) == ["channels", "products"]


def test_database_foreign_keys_join_tables_too() -> None:
    graph = join_graph([], "safe.", [ForeignKey("orders", "shop_id", "shops", "id")])
    assert graph == {"orders": {"shops"}, "shops": {"orders"}}


# Entity values

def test_entity_values_come_from_the_marked_columns() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE channels (id INTEGER, name TEXT)"))
        conn.execute(text("INSERT INTO channels VALUES (1, '直播带货'), (2, '直播带货'), "
                          "(3, NULL), (4, '自然搜索')"))
        items = [SchemaItem(table_name="main.channels", column_name="name", description="",
                            is_entity=1),
                 SchemaItem(table_name="main.channels", column_name="id", description="")]
        values = repository.fetch_entity_values(conn, items)
    assert sorted(row["value"] for row in values) == ["直播带货", "自然搜索"]
    assert values[0] == {"entity_type": "channels.name", "value": values[0]["value"],
                         "table_name": "main.channels", "column_name": "name"}


def test_entity_columns_must_be_plain_names() -> None:
    engine = create_engine("sqlite://")
    item = SchemaItem(table_name="channels; DROP TABLE x", column_name="name",
                      description="", is_entity=1)
    with engine.begin() as conn, pytest.raises(ValueError, match="不合法"):
        repository.fetch_entity_values(conn, [item])


def test_the_seed_marks_the_entity_columns() -> None:
    marked = {(item.table_name, item.column_name) for item in ITEMS if item.is_entity}
    assert marked == {("safe.products", "name"), ("safe.categories", "name"),
                      ("safe.channels", "name"), ("safe.orders", "province")}


# The rerank adapter

class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _post(monkeypatch: pytest.MonkeyPatch, response: Any) -> list[dict]:
    sent: list[dict] = []

    def post(url: str, json: dict, timeout: float, headers: dict) -> Any:
        sent.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(httpx, "post", post)
    return sent


RERANKER = DashScopeReranker("https://rerank.test/api", "qwen3-rerank", "sk-secret", 5.0)


def test_dashscope_sends_the_native_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _post(monkeypatch, _Response(200, {"output": {"results": [
        {"index": 2, "relevance_score": 0.3}, {"index": 0, "relevance_score": 0.9}]}}))
    results = RERANKER.rerank("各渠道", ["a", "b", "c"], 5)
    assert results == [RerankResult(0, 0.9), RerankResult(2, 0.3)]
    assert sent[0]["json"] == {"model": "qwen3-rerank",
                               "input": {"query": "各渠道", "documents": ["a", "b", "c"]},
                               "parameters": {"top_n": 3}}
    assert sent[0]["headers"] == {"Authorization": "Bearer sk-secret"}
    assert sent[0]["timeout"] == 5.0


@pytest.mark.parametrize("response, message", [
    (_Response(401, {"code": "InvalidApiKey", "message": "Invalid API-key provided."}),
     "重排模型返回 HTTP 401：InvalidApiKey Invalid API-key provided."),
    (_Response(502, ValueError("no json")), "重排模型返回的不是 JSON：HTTP 502"),
    (_Response(200, {"output": {}}), "重排模型返回的格式不对"),
    (httpx.ConnectTimeout("timed out"), "重排模型请求失败：ConnectTimeout"),
])
def test_dashscope_errors_say_what_went_wrong(monkeypatch: pytest.MonkeyPatch, response: Any,
                                              message: str) -> None:
    _post(monkeypatch, response)
    with pytest.raises(RerankError) as caught:
        RERANKER.rerank("q", ["a"], 1)
    assert str(caught.value).startswith(message)
    assert "sk-secret" not in str(caught.value)


def test_dashscope_with_nothing_to_rank_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _post(monkeypatch, _Response(200, {}))
    assert RERANKER.rerank("q", [], 3) == []
    assert sent == []


def test_build_reranker_follows_the_settings() -> None:
    assert build_reranker(Settings(_env_file=None, rerank_provider="off")) is None
    assert isinstance(build_reranker(Settings(_env_file=None, rerank_provider="fake")),
                      FakeReranker)
    assert build_reranker(Settings(_env_file=None, rerank_provider="qwen",
                                   embedding_api_key="")) is None
    reranker = build_reranker(Settings(_env_file=None, rerank_provider="qwen",
                                       embedding_api_key="sk-embedding"))
    assert isinstance(reranker, DashScopeReranker)
    assert reranker.model_name == "qwen3.7-text-rerank"
    assert reranker.endpoint.endswith("/services/rerank/text-rerank/text-rerank")
    assert reranker._api_key == "sk-embedding"
    own = build_reranker(Settings(_env_file=None, rerank_provider="qwen", rerank_api_key="sk-r",
                                  rerank_model="qwen3-rerank", embedding_api_key="sk-e"))
    assert isinstance(own, DashScopeReranker)
    assert own._api_key == "sk-r" and own.model_name == "qwen3-rerank"


def test_the_fake_reranker_prefers_shared_words() -> None:
    results = FakeReranker().rerank("渠道订单", ["广告花费", "订单渠道"], 2)
    assert [result.index for result in results] == [1, 0]


def test_a_traced_search_records_its_embedding_and_rerank_calls(semantic: None) -> None:
    from app.agent.trace import CallTrace, text_hash

    class LimitedReranker:
        model_name = "limited"

        def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
            raise RerankError("重排模型返回 HTTP 429：Throttling")

    trace = CallTrace()
    searcher = _searcher(reranker=OrderReranker(["orders"]))
    searcher.trace = trace
    searcher.search("各渠道的订单数", None, use_keyword=False, complete=False)  # type: ignore[arg-type]
    failing = _searcher(reranker=LimitedReranker())
    failing.trace = trace
    failing.search("各渠道的订单数", None, use_keyword=False, complete=False)  # type: ignore[arg-type]

    kinds = [(record.kind, record.name, record.ok, record.error_type) for record in trace.records]
    assert kinds == [("embedding", "stub-embedding", True, ""), ("rerank", "order-rerank", True, ""),
                     ("embedding", "stub-embedding", True, ""), ("rerank", "limited", False, "429")]
    assert trace.records[0].input_hash == text_hash("各渠道的订单数")
    # The same query and candidates send the same rerank input.
    assert trace.records[1].input_hash == trace.records[3].input_hash
    assert "HTTP 429" in trace.records[3].detail
