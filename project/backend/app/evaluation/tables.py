"""The evaluation tables, kept apart from the evaluation code so start-up and the admin
commands can create them without loading it."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

EVAL_TABLES = ("eval_runs", "eval_case_results")

# Same statements as data/initdb/02_app_schema.sql; start-up and grant_eval_user run them
# for databases made before them.
EVAL_RUNS_DDL = """CREATE TABLE IF NOT EXISTS eval_runs (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    dataset       VARCHAR(64) NOT NULL,
    mode          VARCHAR(16) NOT NULL,
    categories    JSON NULL,
    options       JSON NULL,
    repeat_count  INT NOT NULL DEFAULT 1,
    concurrency   INT NOT NULL DEFAULT 1,
    status        VARCHAR(16) NOT NULL,
    done          INT NOT NULL DEFAULT 0,
    total         INT NOT NULL DEFAULT 0,
    llm_model     VARCHAR(64) NOT NULL DEFAULT '',
    schema_mode   VARCHAR(16) NOT NULL DEFAULT '',
    git_commit    VARCHAR(40) NOT NULL DEFAULT '',
    summary       JSON NULL,
    error         TEXT NULL,
    started_at    DATETIME NOT NULL,
    finished_at   DATETIME NULL,
    KEY idx_eval_runs_status (status)
) ENGINE=InnoDB;"""

EVAL_CASE_RESULTS_DDL = """CREATE TABLE IF NOT EXISTS eval_case_results (
    id             BIGINT PRIMARY KEY AUTO_INCREMENT,
    run_id         BIGINT NOT NULL,
    case_id        VARCHAR(64) NOT NULL,
    attempt        INT NOT NULL DEFAULT 1,
    dataset        VARCHAR(64) NOT NULL DEFAULT '',
    kind           VARCHAR(32) NOT NULL DEFAULT '',
    category       VARCHAR(32) NOT NULL DEFAULT '',
    passed         TINYINT(1) NOT NULL,
    scored         TINYINT(1) NOT NULL DEFAULT 1,
    detail         TEXT NULL,
    failure_hint   VARCHAR(64) NOT NULL DEFAULT '',
    sql_text       TEXT NULL,
    summary        TEXT NULL,
    summary_source VARCHAR(16) NOT NULL DEFAULT '',
    warnings       JSON NULL,
    log_id         BIGINT NULL,
    input_tokens   INT NOT NULL DEFAULT 0,
    output_tokens  INT NOT NULL DEFAULT 0,
    duration_ms    INT NOT NULL DEFAULT 0,
    result         JSON NOT NULL,
    created_at     DATETIME NOT NULL,
    UNIQUE KEY uk_eval_case (run_id, case_id, attempt)
) ENGINE=InnoDB;"""

def ensure_eval_tables(conn: Connection) -> None:
    conn.execute(text(EVAL_RUNS_DDL))
    conn.execute(text(EVAL_CASE_RESULTS_DDL))
