from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .. import demo_data, importer
from ..agent import session
from ..agent.scope_model import build_chat_model, probe_chat_model
from ..config import get_settings
from ..db import accounts
from ..db.engine import app_connection, business_connection
from ..embedding import build_embedder
from ..embedding.base import EmbeddingError
from ..evaluation.tables import ensure_eval_tables
from ..vector import build_vector_store
from . import indexer, repository


def cmd_init_semantic() -> int:
    settings = get_settings()
    bundle = repository.load_seed(settings.seed_dir)
    with app_connection(settings) as conn:
        counts = repository.sync_seed(conn, bundle)
    print(f"semantic layer synced: {counts}")
    return 0


def cmd_import_dataset(manifest_path: str | None) -> int:
    if not manifest_path:
        print("需要 --manifest，例如 data/dabench/manifest.json", file=sys.stderr)
        return 1
    settings = get_settings()
    try:
        manifest = importer.load_manifest(Path(manifest_path))
    except (OSError, KeyError, ValueError) as exc:
        print(f"清单读取失败：{exc}", file=sys.stderr)
        return 1
    started = time.perf_counter()

    def progress(table: str, rows: int) -> None:
        print(f"\r导入 {table}：{rows} 行", end="", flush=True)

    try:
        counts = importer.import_dataset(settings, manifest,
                                         settings.data_dir / "initdb" / "02_app_schema.sql",
                                         progress)
    except ValueError as exc:
        # DatasetError, or a bad app account name or empty password.
        print(f"\n导入失败：{exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"\n导入失败，数据库报错：{getattr(exc, 'orig', None) or exc}", file=sys.stderr)
        return 1
    print(f"\n导入完成，用时 {time.perf_counter() - started:.0f} 秒：{counts}")
    print(f"原始表在 {manifest.raw_database}，只读账号查 {manifest.database} 的视图，"
          f"应用库是 {manifest.app_database}")
    if settings.mysql_eval_password:
        print(f"评测账号 {settings.mysql_eval_user} 已授权读 {manifest.database} "
              f"和 {manifest.app_database}")
    else:
        print("没有设置 MYSQL_EVAL_PASSWORD，跳过了评测账号的授权；"
              "设置后用这个数据集的配置运行 grant_eval_user")
    return 0


def cmd_grant_app_user() -> int:
    """Create the app account and grant it this instance's databases, as the admin account.

    New databases get the account from data/initdb/03_grants.sql; databases made before
    it existed, or with another MYSQL_APP_PASSWORD, need this once.
    """
    settings = get_settings()
    demo_database = demo_data.DEMO_DATABASE if settings.generate_demo_data else None
    engine = create_engine(settings.admin_server_url.render_as_string(hide_password=False))
    try:
        with engine.begin() as conn:
            statements = accounts.ensure_app_user(conn, settings.mysql_app_user,
                                                  settings.mysql_app_password,
                                                  settings.mysql_app_db, demo_database)
    except ValueError as exc:
        print(f"授权失败：{exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"授权失败，数据库报错（用的是管理账号 {settings.mysql_admin_user}）："
              f"{getattr(exc, 'orig', None) or exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(f"应用账号 {settings.mysql_app_user} 已就绪：")
    for statement in statements:
        print(f"  {statement}")
    return 0


def cmd_grant_eval_user() -> int:
    """Create the evaluation account and let it read this instance's views and app
    database and write the evaluation tables, as the admin account. Run once per
    instance, e.g. again for DABench. Creates the evaluation tables first if missing."""
    settings = get_settings()
    if not settings.mysql_eval_password:
        print("授权失败：评测账号的密码为空，请在 .env 设置 MYSQL_EVAL_PASSWORD", file=sys.stderr)
        return 1
    engine = create_engine(settings.admin_server_url.render_as_string(hide_password=False))
    try:
        with engine.begin() as conn:
            conn.execute(text(f"USE `{accounts.checked_name(settings.mysql_app_db, '应用库名')}`"))
            ensure_eval_tables(conn)
            statements = accounts.ensure_eval_user(conn, settings.mysql_eval_user,
                                                   settings.mysql_eval_password,
                                                   settings.mysql_business_db,
                                                   settings.mysql_app_db)
    except ValueError as exc:
        print(f"授权失败：{exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"授权失败，数据库报错（用的是管理账号 {settings.mysql_admin_user}）："
              f"{getattr(exc, 'orig', None) or exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(f"评测账号 {settings.mysql_eval_user} 已就绪：")
    for statement in statements:
        print(f"  {statement}")
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
        kind = row.get("error_type") or ""
        state = "" if row["ok"] else (f" 失败({kind})" if kind else " 失败")
        detail = f" {row['detail']}" if row["detail"] else ""
        lines.append(f"{row['seq']:>3}  +{row['started_ms'] / 1000:6.1f}s  "
                     f"{row['duration_ms'] / 1000:5.1f}s  {row['agent']:<12} {row['kind']:<9} "
                     f"{row['name']}{tokens}{state}{detail}")
    return lines


PREVIEW_PRINT_ROWS = 5


def format_answer(log: dict) -> list[str]:
    """The conclusion as the user saw it, with where it came from and its warnings."""
    lines = [f"问题：{log['question']}",
             f"状态 {log['status']}，来源 {log['source']}，耗时 {log['duration_ms'] / 1000:.1f}s"]
    if log["summary"]:
        lines.append(f"结论（{log['summary_source'] or '-'}）：{log['summary']}")
    else:
        lines.append("结论：（无）")
    if log["error_message"]:
        lines.append(f"错误：{log['error_message']}")
    lines.extend(f"警告：{warning}" for warning in log["warnings"])
    intent = log.get("intent") or {}
    if intent.get("text"):
        lines.append(f"理解为：{intent['text']}")
    lines.extend(f"假设：{item}" for item in intent.get("assumptions") or [])
    conclusion = log.get("conclusion") or {}
    for number, point in enumerate(conclusion.get("points") or [], start=1):
        cited = "；".join(
            f"查询{item['query']}"
            + (f" 第{'、'.join(map(str, item.get('rows') or []))}行" if item.get("rows") else "")
            + (f" 列 {'、'.join(item.get('columns') or [])}" if item.get("columns") else "")
            for item in point.get("evidence") or [])
        lines.append(f"要点 {number}：{point.get('claim', '')}  依据：{cited or '（无）'}")
    if conclusion and not conclusion.get("checked", True):
        lines.append("依据：重写一次后仍有不对的引用，已去掉，页面展示全部查询")
    clarify = intent.get("clarify") or {}
    if clarify.get("question"):
        options = "、".join(clarify.get("options") or [])
        lines.append(f"反问（第 {clarify.get('round', 1)} 轮）：{clarify['question']}"
                     + (f"  可选：{options}" if options else ""))
    return lines


def format_executions(records: list[dict]) -> list[str]:
    """Every SQL the question tried, with its first rows."""
    lines = []
    for record in records:
        number = f"查询{record['query_no']}" if record["query_no"] else "未计入"
        lines.append(f"#{record['seq']} [{number}] {record['status']} "
                     f"{record['duration_ms']}ms {record['row_count']} 行  "
                     f"{record['purpose'] or '-'}")
        lines.extend(f"    {line}" for line in record["sql_text"].splitlines())
        if record["error"]:
            lines.append(f"    错误：{record['error']}")
        preview = record["rows_preview"]
        if preview:
            lines.append(f"    | {' | '.join(str(name) for name in preview['columns'])}")
            for row in preview["rows"][:PREVIEW_PRINT_ROWS]:
                lines.append(f"    | {' | '.join(str(value) for value in row)}")
            hidden = len(preview["rows"]) - PREVIEW_PRINT_ROWS
            if hidden > 0:
                lines.append(f"    | …… 另有 {hidden} 行存档")
    return lines


def cmd_show_trace(log_id: int | None) -> int:
    if log_id is None:
        print("用法：show_trace --log-id <query_logs 的 id>")
        return 2
    with app_connection(get_settings()) as conn:
        log = session.load_query_log(conn, log_id)
        executions = session.load_query_executions(conn, log_id)
        rows = session.load_call_traces(conn, log_id)
    if log is None:
        print(f"没有 log_id={log_id} 的问答记录")
        return 1
    for line in format_answer(log):
        print(line)
    print()
    if executions:
        print(f"查询 {len(executions)} 条：")
        for line in format_executions(executions):
            print(line)
    else:
        print("没有查询记录")
    print()
    if not rows:
        print("没有调用记录")
        return 0
    for line in format_trace(rows):
        print(line)
    models = [row for row in rows if row["kind"] == "model"]
    tools = [row for row in rows if row["kind"] == "tool"]
    print(f"模型 {len(models)} 次，tokens {sum(row['input_tokens'] for row in models)}"
          f"/{sum(row['output_tokens'] for row in models)}，工具 {len(tools)} 次")
    backend = {kind: [row for row in rows if row["kind"] == kind]
               for kind in ("retrieval", "embedding", "rerank", "sql")}
    if any(backend.values()):
        print("后端调用：" + "，".join(
            f"{kind} {len(items)} 次 {sum(row['duration_ms'] for row in items) / 1000:.1f}s"
            for kind, items in backend.items() if items))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.semantic.cli")
    parser.add_argument("command",
                        choices=["init_semantic", "build_index", "status", "check_models",
                                 "show_trace", "import_dataset", "grant_app_user",
                                 "grant_eval_user"])
    parser.add_argument("--log-id", type=int, help="show_trace 要查看的 query_logs id")
    parser.add_argument("--manifest", help="import_dataset 用的数据集清单")
    args = parser.parse_args(argv)
    if args.command == "show_trace":
        return cmd_show_trace(args.log_id)
    if args.command == "import_dataset":
        return cmd_import_dataset(args.manifest)
    if args.command == "grant_app_user":
        return cmd_grant_app_user()
    if args.command == "grant_eval_user":
        return cmd_grant_eval_user()
    if args.command == "init_semantic":
        return cmd_init_semantic()
    if args.command == "build_index":
        return cmd_build_index()
    if args.command == "check_models":
        return cmd_check_models()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())