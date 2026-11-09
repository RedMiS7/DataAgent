"""Loading a dataset into MySQL: manifest, conversion, SQL; and the DABench files."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app import bootstrap
from app.agent import planner, router
from app.agent.pipeline import (
    AGENTIC_RULE_COLUMNS,
    ANALYSIS_PROMPT,
    DIRECT_PROMPT,
    REFUSAL_MESSAGE,
    SYSTEM_PROMPT,
)
from app.config import Settings
from app.importer import (
    Column,
    DatasetError,
    Table,
    app_schema_statements,
    convert,
    create_table_sql,
    load_manifest,
    table_rows,
    view_sql,
)
from app.semantic import repository
from app.semantic.cli import main as cli

PROJECT_DIR = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_DIR / "data" / "dabench" / "manifest.json"
SEED = PROJECT_DIR / "data" / "seed_dabench"


# Converting values

@pytest.mark.parametrize("value, kind, expected", [
    ("2023/1/4", "date", date(2023, 1, 4)),
    ("2024-03-12", "date", date(2024, 3, 12)),
    (datetime(2023, 4, 9, 0, 0), "date", date(2023, 4, 9)),
    (" 是 ", "bool", 1),
    ("否", "bool", 0),
    (1, "bool", 1),
    ("12", "int", 12),
    (604000, "int", 604000),
    (12.0, "int", 12),
    ("28.40489801", "decimal(16,8)", Decimal("28.40489801")),
    (13.62, "decimal(6,2)", Decimal("13.62")),
    (" Uttarakhand", "varchar(64)", "Uttarakhand"),
    (918055, "varchar(10)", "918055"),
    (datetime(2023, 1, 9), "varchar(64)", "2023-01-09T00:00:00"),
    ("", "int", None),
    ("   ", "date", None),
    (None, "varchar(8)", None),
])
def test_values_are_converted_to_the_column_type(value: Any, kind: str, expected: Any) -> None:
    assert convert(value, kind) == expected


@pytest.mark.parametrize("value, kind, message", [
    ("2023年1月", "date", "不是日期"),
    ("也许", "bool", "不是是/否"),
    ("4.5", "int", "不是整数"),
    ("abc", "int", "不是整数"),
    ("abc", "decimal(6,2)", "不是数字"),
    ("12345", "decimal(6,2)", r"超出 decimal\(6,2\) 的范围"),
    ("很长的文字", "varchar(4)", "超过 4 个字符"),
])
def test_bad_values_are_rejected(value: Any, kind: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        convert(value, kind)


# The manifest

def _manifest(tmp_path: Path, **table: Any) -> Path:
    data = {"raw_database": "demo_raw", "database": "demo", "app_database": "demo_app",
            "data_dir": ".",
            "tables": [{"name": "t", "source": "t.csv",
                        "columns": [{"source": "编号", "name": "id", "type": "int"},
                                    {"source": "日期 ", "name": "day", "type": "date"},
                                    {"source": "手机号", "name": "phone", "type": "varchar(20)"}],
                        "primary_key": ["id"], "hidden": ["phone"], **table}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_manifest_is_read_with_paths_next_to_it(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    table = manifest.tables[0]
    assert manifest.data_dir == tmp_path.resolve()
    assert table.columns[1] == Column("日期", "day", "date")
    assert [column.name for column in table.visible] == ["id", "day"]


@pytest.mark.parametrize("change, message", [
    ({"name": "Bad-Name"}, "表名只能用小写字母"),
    ({"primary_key": ["nope"]}, "没有这些字段：nope"),
    ({"hidden": ["nope"]}, "没有这些字段：nope"),
    ({"indexes": [["day", "nope"]]}, "没有这些字段：nope"),
    ({"columns": [{"source": "a", "name": "x", "type": "text"}]}, "类型不支持"),
    ({"columns": [{"source": "a", "name": "x", "type": "int"},
                  {"source": "b", "name": "x", "type": "int"}]}, "重名的字段"),
])
def test_a_wrong_manifest_says_what_is_wrong(tmp_path: Path, change: dict, message: str) -> None:
    if "columns" in change:
        change = {**change, "primary_key": [], "hidden": []}
    with pytest.raises(DatasetError, match=message):
        load_manifest(_manifest(tmp_path, **change))


# Reading files

def test_csv_rows_are_converted_and_blank_lines_skipped(tmp_path: Path) -> None:
    (tmp_path / "t.csv").write_text("﻿编号,日期 ,手机号\n1,2023/1/4,138****0000\n\n2,,\n",
                                    encoding="utf-8")
    table = load_manifest(_manifest(tmp_path)).tables[0]
    assert list(table_rows(table, tmp_path)) == [
        {"id": 1, "day": date(2023, 1, 4), "phone": "138****0000"},
        {"id": 2, "day": None, "phone": None},
    ]


def test_a_bad_value_names_the_file_line_and_column(tmp_path: Path) -> None:
    (tmp_path / "t.csv").write_text("编号,日期,手机号\n1,2023/1/4,x\n2,昨天,y\n", encoding="utf-8")
    table = load_manifest(_manifest(tmp_path)).tables[0]
    with pytest.raises(DatasetError, match=r"t\.csv 第 3 行，日期：不是日期：'昨天'"):
        list(table_rows(table, tmp_path))


def test_an_empty_key_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "t.csv").write_text("编号,日期,手机号\n,2023/1/4,x\n", encoding="utf-8")
    table = load_manifest(_manifest(tmp_path)).tables[0]
    with pytest.raises(DatasetError, match="第 2 行，编号：主键不能为空"):
        list(table_rows(table, tmp_path))


def test_missing_columns_and_files_are_reported(tmp_path: Path) -> None:
    table = load_manifest(_manifest(tmp_path)).tables[0]
    with pytest.raises(DatasetError, match="找不到数据文件"):
        list(table_rows(table, tmp_path))
    (tmp_path / "t.csv").write_text("编号,日期\n1,2023/1/4\n", encoding="utf-8")
    with pytest.raises(DatasetError, match=r"t\.csv 里没有这些列：手机号"):
        list(table_rows(table, tmp_path))


def test_excel_rows_come_from_the_named_sheet(tmp_path: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.active.title = "汇总"
    sheet = workbook.create_sheet("明细")
    sheet.append(["编号 ", "日期", "手机号"])
    sheet.append([7, datetime(2023, 9, 23), None])
    workbook.save(tmp_path / "t.xlsx")
    table = load_manifest(_manifest(tmp_path, source="t.xlsx", sheet="明细")).tables[0]
    assert list(table_rows(table, tmp_path)) == [{"id": 7, "day": date(2023, 9, 23),
                                                  "phone": None}]


# SQL

TABLE = Table(name="t", source="t.csv",
              columns=[Column("编号", "id", "int"), Column("价格", "price", "decimal(12,2)"),
                       Column("手机号", "phone", "varchar(20)")],
              primary_key=["id"], indexes=[["price"]], hidden=["phone"])


def test_the_raw_table_and_the_view() -> None:
    sql = create_table_sql("demo_raw", TABLE)
    assert sql.startswith("CREATE TABLE `demo_raw`.`t` (")
    assert "`id` BIGINT NOT NULL" in sql and "`price` DECIMAL(12,2) NULL" in sql
    assert "PRIMARY KEY (`id`)" in sql and "KEY `idx_price` (`price`)" in sql
    assert view_sql("demo_raw", "demo", TABLE) == (
        "CREATE OR REPLACE VIEW `demo`.`t` AS SELECT `id`, `price` FROM `demo_raw`.`t`")


def test_the_app_schema_without_its_own_database() -> None:
    statements = app_schema_statements(PROJECT_DIR / "data" / "initdb" / "02_app_schema.sql")
    assert statements and all(s.startswith("CREATE TABLE IF NOT EXISTS") for s in statements)
    assert any("semantic_schema_items" in s and "is_entity" in s for s in statements)
    assert not any("dataagent" in s for s in statements)


def test_the_command_needs_a_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["import_dataset"]) == 1
    assert "需要 --manifest" in capsys.readouterr().err


# DABench

def test_the_dabench_manifest() -> None:
    manifest = load_manifest(MANIFEST)
    assert (manifest.raw_database, manifest.database, manifest.app_database) == (
        "dabench_raw", "dabench", "dataagent_dabench")
    assert [table.name for table in manifest.tables] == [
        "inventory", "orders", "car_leads", "loan_applications"]
    hidden = {table.name: table.hidden for table in manifest.tables}
    assert hidden["car_leads"] == ["phone", "customer_name"]
    assert hidden["loan_applications"] == ["address", "street", "district", "postal_code"]


def test_the_dabench_semantic_layer_matches_the_views() -> None:
    manifest = load_manifest(MANIFEST)
    bundle = repository.load_seed(SEED)
    visible = {(f"dabench.{table.name}", column.name)
               for table in manifest.tables for column in table.visible}
    described = {(item.table_name, item.column_name) for item in bundle.schema_items}
    # Every column the read-only account sees is described, and nothing hidden is.
    assert described == visible
    assert {table.table_name for table in bundle.tables} == {
        f"dabench.{table.name}" for table in manifest.tables}
    for metric in bundle.metrics:
        assert {part.strip() for part in metric.related_tables.split(",")} <= {
            f"dabench.{table.name}" for table in manifest.tables}, metric.name
    assert {item.column_name for item in bundle.schema_items if item.is_entity} >= {
        "return_reason", "intended_model", "sub_channel", "loan_type", "product_id"}


def test_the_dabench_metrics_follow_the_question_definitions() -> None:
    metrics = {metric.name: metric for metric in repository.load_seed(SEED).metrics}
    assert "interest_rate > 12" in metrics["高利贷"].formula
    assert "DATEDIFF(c.order_date, c.created_date) <= 7" in metrics["闪电单"].formula
    assert "c.order_status = '已交车'" in metrics["成单数"].formula
    assert "(订单利润 − 线索价格) / 线索价格" in metrics["线索ROI"].definition


@pytest.mark.skipif(not (PROJECT_DIR / "DB" / "data" / "order.csv").exists(),
                    reason="DABench 的数据文件没有下载（project/DB/data 不提交）")
def test_every_dabench_file_converts() -> None:
    manifest = load_manifest(MANIFEST)
    counts = {table.name: sum(1 for _ in table_rows(table, manifest.data_dir))
              for table in manifest.tables}
    assert counts == {"inventory": 73100, "orders": 63044, "car_leads": 17820,
                      "loan_applications": 50000}


# Settings for another dataset

def test_the_seed_folder_follows_the_setting() -> None:
    assert Settings(_env_file=None).seed_dir.name == "seed"
    assert Settings(_env_file=None, semantic_seed="seed_dabench").seed_dir.name == "seed_dabench"


def test_demo_data_can_be_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(settings: Settings) -> bool:
        raise AssertionError("must not look for shop orders")

    monkeypatch.setattr(bootstrap.demo_data, "has_orders", fail)
    settings = Settings(_env_file=None, generate_demo_data=False,
                        local_env_file=tmp_path / ".env.local")
    runner = bootstrap.Bootstrapper(settings, None)  # type: ignore[arg-type]
    assert runner._prepare_demo_data() == ("skipped", "这个数据集不生成演示数据")


# Prompts with no shop in them

SHOP_WORDS = ("电商", "卖家", "GMV", "类目", "商品", "退款", "广告", "物流", "客单价", "运费")


@pytest.mark.parametrize("prompt", [
    SYSTEM_PROMPT, ANALYSIS_PROMPT, DIRECT_PROMPT, AGENTIC_RULE_COLUMNS, REFUSAL_MESSAGE,
    router.ROUTER_PROMPT, planner.PLAN_PROMPT, planner.SUPPLEMENT_PROMPT,
    planner.STEP_ANSWER_RULE, planner.SUMMARY_PROMPT,
])
def test_the_prompts_name_no_shop(prompt: str) -> None:
    assert [word for word in SHOP_WORDS if word in prompt] == []
