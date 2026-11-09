"""The tables put into the prompt, each with all of its columns.

When every table fits in the budget, all of them go in. Otherwise only the tables
this question needs: those retrieval found, those the recent SQL used, and the
tables that join them. Columns are never cut from a table.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .models import SchemaItem
from .repository import ForeignKey

# Table name -> [(column, type)], as the database reports them, in column order.
TableColumns = Mapping[str, Sequence[tuple[str, str]]]

JOIN_HINT = re.compile(r"关联\s*([\w.]+)")

HEADER = "【表结构】下面每张表都列出了全部字段（字段名、类型、说明），没列出的字段就不存在。"
REST_HEADER = "其他表（没有列出字段，要用时先调用 describe_table）："


@dataclass(frozen=True, slots=True)
class SchemaContext:
    text: str
    # The tables whose columns are listed, without the schema prefix.
    tables: list[str]
    # Every table of the database is listed.
    complete: bool
    tokens: int


def estimate_tokens(text: str) -> int:
    """A generous estimate: a Chinese character is at most one token, and English
    takes about three characters per token."""
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return len(text) - ascii_chars + ascii_chars // 3 + 1


def bare_name(name: str, prefix: str) -> str:
    return name.strip().strip("`").lower().removeprefix(prefix)


def table_names(sql: str, schema: str) -> list[str]:
    """The tables a SQL names as ``schema.table``, in order."""
    found = re.findall(rf"\b{re.escape(schema)}\.(\w+)\b", sql, re.IGNORECASE)
    return list(dict.fromkeys(name.lower() for name in found))


def column_line(column: str, column_type: str, item: SchemaItem | None) -> str:
    line = f"- {column}（{column_type}）"
    if item is None:
        return line
    line += f"：{item.description}"
    if item.synonyms:
        line += f"（同义词：{item.synonyms}）"
    if item.join_hint:
        line += f"（{item.join_hint}）"
    return line


def render_table(table: str, columns: Sequence[tuple[str, str]],
                 items: Mapping[tuple[str, str], SchemaItem], prefix: str,
                 summary: str = "") -> str:
    title = f"{prefix}{table}：" + summary
    lines = [column_line(column, column_type, items.get((f"{prefix}{table}", column)))
             for column, column_type in columns]
    return "\n".join([title, *lines])


def hint_keys(items: Iterable[SchemaItem], prefix: str) -> list[ForeignKey]:
    """Links between columns written in the join hints ("关联 safe.orders.id")."""
    keys: list[ForeignKey] = []
    for item in items:
        source = bare_name(item.table_name, prefix)
        for match in JOIN_HINT.finditer(item.join_hint):
            parts = match.group(1).lower().split(".")
            if len(parts) < 2:
                continue
            keys.append(ForeignKey(source, item.column_name.lower(), parts[-2], parts[-1]))
    return keys


def join_graph(items: Iterable[SchemaItem], prefix: str,
               keys: Iterable[ForeignKey] = ()) -> dict[str, set[str]]:
    """Which tables join which: the database's foreign keys and the join hints."""
    graph: dict[str, set[str]] = {}
    for key in [*keys, *hint_keys(items, prefix)]:
        if key.ref_table != key.table:
            graph.setdefault(key.table, set()).add(key.ref_table)
            graph.setdefault(key.ref_table, set()).add(key.table)
    return graph


def pointed_tables(columns: Iterable[tuple[str, str]], keys: Iterable[ForeignKey]) -> list[str]:
    """The tables these (table, column) pairs link to: a question about "各渠道" matches
    orders.channel_id, and the names are in channels."""
    links: dict[tuple[str, str], list[str]] = {}
    for key in keys:
        links.setdefault((key.table, key.column), []).append(key.ref_table)
    found: list[str] = []
    for table, column in columns:
        for target in links.get((table, column.lower()), []):
            if target != table and target not in found:
                found.append(target)
    return found


def _path_to(graph: Mapping[str, set[str]], start: str, reached: set[str]) -> list[str]:
    """The shortest path from ``start`` to any table in ``reached``, without the end;
    just ``start`` when nothing joins them."""
    previous: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        table = queue.popleft()
        if table in reached:
            path: list[str] = []
            node = previous[table]
            while node is not None:
                path.append(node)
                node = previous[node]
            return path[::-1]
        for neighbour in sorted(graph.get(table, ())):
            if neighbour not in previous:
                previous[neighbour] = table
                queue.append(neighbour)
    return [start]


def connect(tables: Sequence[str], graph: Mapping[str, set[str]]) -> list[str]:
    """``tables`` in order, with the tables between them added: orders and categories
    need order_items and products to join."""
    result: list[str] = []
    for table in tables:
        if table in result:
            continue
        if not result:
            result.append(table)
            continue
        for step in _path_to(graph, table, set(result)):
            if step not in result:
                result.append(step)
    return result


def build_schema_context(columns: TableColumns, items: Iterable[SchemaItem],
                         summaries: Mapping[str, str], wanted: Sequence[str],
                         budget: int, prefix: str) -> SchemaContext:
    """All tables when they fit in ``budget`` tokens; otherwise the ``wanted`` ones
    (most needed first) and the tables joining them, as many as fit."""
    items = list(items)
    by_key = {(item.table_name, item.column_name): item for item in items}
    sections = {table: render_table(table, table_columns, by_key, prefix,
                                    summaries.get(table, ""))
                for table, table_columns in columns.items()}
    everything = "\n\n".join([HEADER, *sections.values()])
    tokens = estimate_tokens(everything)
    if tokens <= budget:
        return SchemaContext(everything, list(sections), True, tokens)

    graph = join_graph(items, prefix)
    known = [bare_name(table, prefix) for table in wanted]
    chosen = [table for table in connect([t for t in known if t in sections], graph)
              if table in sections]
    kept: list[str] = []
    used = estimate_tokens(HEADER)
    for table in chosen:
        cost = estimate_tokens(sections[table])
        if kept and used + cost > budget:
            break
        kept.append(table)
        used += cost
    rest = [table for table in sections if table not in kept]
    parts = [HEADER, *(sections[table] for table in kept)]
    if rest:
        parts.append(REST_HEADER + "\n" + "\n".join(
            f"- {prefix}{table}" + (f"：{summaries[table]}" if summaries.get(table) else "")
            for table in rest))
    text = "\n\n".join(parts)
    return SchemaContext(text, kept, False, estimate_tokens(text))
