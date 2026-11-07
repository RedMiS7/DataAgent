from __future__ import annotations

import argparse
import sys
import time

from ..agent import session
from ..agent.scope_model import build_chat_model, probe_chat_model
from ..config import get_settings
from ..db.engine import app_connection, business_connection
from ..embedding import build_embedder
from ..embedding.base import EmbeddingError
from ..vector import build_vector_store
from . import indexer, repository


def cmd_init_semantic() -> int:
    settings = get_settings()
    bundle = repository.load_seed(settings.seed_dir)
    with app_connection(settings) as conn:
        counts = repository.sync_seed(conn, bundle)
    print(f"semantic layer synced: {counts}")
    return 0


def cmd_build_index() -> int:
    settings = get_settings()
    store = build_vector_store(settings)
    if store is None:
        print(f"Qdrant 不可用：{settings.qdrant_url}", file=sys.stderr)
        return 1
    embedder = build_embedder(settings)
    if embedder is None:
        print("未配置 embedding endpoint/model，无法构建索引", file=sys.stderr)
        return 1
    with app_connection(settings) as app_conn, business_connection(settings) as business_conn:
        counts = indexer.build_index(settings, app_conn, business_conn, store, embedder)
    print(f"index built with {embedder.model_name}: {counts}")
    return 0


def cmd_check_models() -> int:
    settings = get_settings()
    ok = True

    model = build_chat_model(settings)
    if model is None:
        print("LLM 未配置：设置 LLM_API_KEY，或使用内置预设（LLM_PROVIDER=deepseek）",
              file=sys.stderr)
        ok = False
    elif settings.llm_provider.lower() == "fake":
        print("LLM 使用离线脚本模型，跳过联网检查")
    else:
        started = time.perf_counter()
        try:
            text = probe_chat_model(model)
            elapsed = int((time.perf_counter() - started) * 1000)
            print(f"LLM {model.model} OK ({elapsed} ms): {text[:40]}")
        except Exception as exc:
            print(f"LLM {model.model} 调用失败：{exc}", file=sys.stderr)
            ok = False

    embedder = build_embedder(settings)
    if embedder is None:
        print("Embedding 未配置：设置 EMBEDDING_API_KEY，"
              "或使用内置预设（EMBEDDING_PROVIDER=qwen）", file=sys.stderr)
        ok = False
    else:
        started = time.perf_counter()
        try:
            vectors = embedder.embed(["经营数据助手"])
            elapsed = int((time.perf_counter() - started) * 1000)
            print(f"Embedding {embedder.model_name} OK ({elapsed} ms): dim={len(vectors[0])}")
        except EmbeddingError as exc:
            print(f"Embedding {embedder.model_name} 调用失败：{exc}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def cmd_status() -> int:
    settings = get_settings()
    store = build_vector_store(settings)
    with app_connection(settings) as conn:
        tables = repository.fetch_tables(conn)
        items = repository.fetch_schema_items(conn)
        metrics = repository.fetch_metrics(conn)
        cases = repository.fetch_cases(conn)
    print(f"semantic: tables={len(tables)} columns={len(items)} metrics={len(metrics)} cases={len(cases)}")
    print(f"qdrant: {'up' if store else 'down'}")
    if store is not None:
        for suffix in ("schema_items", "metrics", "qa_cases", "entity_values"):
            print(f"  {suffix}: {store.count(settings.collection_name(suffix))}")
    return 0


def format_trace(rows: list[dict]) -> list[str]:
    """One line per call, in the order the calls started."""
    lines = []
    for row in rows:
        tokens = (f" tokens={row['input_tokens']}/{row['output_tokens']}"
                  if row["kind"] == "model" else "")
        state = "" if row["ok"] else " 失败"
        detail = f" {row['detail']}" if row["detail"] else ""
        lines.append(f"{row['seq']:>3}  +{row['started_ms'] / 1000:6.1f}s  "
                     f"{row['duration_ms'] / 1000:5.1f}s  {row['agent']:<12} {row['kind']:<5} "
                     f"{row['name']}{tokens}{state}{detail}")
    return lines


def cmd_show_trace(log_id: int | None) -> int:
    if log_id is None:
        print("用法：show_trace --log-id <query_logs 的 id>")
        return 2
    with app_connection(get_settings()) as conn:
        rows = session.load_call_traces(conn, log_id)
    if not rows:
        print(f"log_id={log_id} 没有调用记录")
        return 1
    for line in format_trace(rows):
        print(line)
    models = [row for row in rows if row["kind"] == "model"]
    print(f"模型 {len(models)} 次，tokens {sum(row['input_tokens'] for row in models)}"
          f"/{sum(row['output_tokens'] for row in models)}，工具 {len(rows) - len(models)} 次")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.semantic.cli")
    parser.add_argument("command",
                        choices=["init_semantic", "build_index", "status", "check_models",
                                 "show_trace"])
    parser.add_argument("--log-id", type=int, help="show_trace 要查看的 query_logs id")
    args = parser.parse_args(argv)
    if args.command == "show_trace":
        return cmd_show_trace(args.log_id)
    if args.command == "init_semantic":
        return cmd_init_semantic()
    if args.command == "build_index":
        return cmd_build_index()
    if args.command == "check_models":
        return cmd_check_models()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())