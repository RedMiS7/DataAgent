from __future__ import annotations

from pydantic import BaseModel, Field


class TableSemantic(BaseModel):
    table_name: str
    summary: str
    schema_version: str = "v1"


class SchemaItem(BaseModel):
    table_name: str
    column_name: str
    description: str
    synonyms: str = ""
    join_hint: str = ""
    schema_version: str = "v1"
    # 0 keeps a column out of retrieval. describe_table still shows it.
    enabled: int = 1


class Metric(BaseModel):
    name: str
    aliases: str = ""
    definition: str
    formula: str
    related_tables: str = ""
    enabled: int = 1


class MetricHit(BaseModel):
    metric: Metric
    score: float = 0.0
    source: str = "vector"


class SchemaHit(BaseModel):
    item: SchemaItem
    score: float = 0.0
    source: str = "vector"


class CaseHit(BaseModel):
    id: int
    question: str
    sql_text: str
    score: float = 0.0
    source: str = "vector"


class EntityHit(BaseModel):
    entity_type: str
    value: str
    score: float = 0.0
    source: str = "exact"


class RetrievalBundle(BaseModel):
    tables: list[TableSemantic] = Field(default_factory=list)
    schema_items: list[SchemaHit] = Field(default_factory=list)
    metrics: list[MetricHit] = Field(default_factory=list)
    cases: list[CaseHit] = Field(default_factory=list)
    entities: list[EntityHit] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""

    def to_prompt_context(self) -> str:
        parts: list[str] = []
        if self.tables:
            parts.append("【表摘要】\n" + "\n".join(
                f"- {t.table_name}: {t.summary}" for t in self.tables))
        if self.schema_items:
            parts.append("【字段】\n" + "\n".join(
                f"- {h.item.table_name}.{h.item.column_name}: {h.item.description}"
                + (f"（同义词：{h.item.synonyms}）" if h.item.synonyms else "")
                + (f"（{h.item.join_hint}）" if h.item.join_hint else "")
                for h in self.schema_items))
        if self.metrics:
            parts.append("【指标口径】\n" + "\n".join(
                f"- {m.metric.name}: {m.metric.definition}；公式：{m.metric.formula}"
                for m in self.metrics))
        if self.cases:
            parts.append("【历史案例】\n" + "\n".join(
                f"- 问题：{c.question}\n  SQL：{c.sql_text}" for c in self.cases))
        if self.entities:
            parts.append("【匹配到的实体值】\n" + "\n".join(
                f"- {e.entity_type}: {e.value}" for e in self.entities))
        if self.degraded:
            parts.append(f"【注意】检索处于降级模式：{self.degraded_reason}")
        return "\n\n".join(parts)