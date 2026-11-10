-- Application database: semantic layer, cases, sessions and logs.
-- The vector store only holds rebuildable indexes; source text lives here.

CREATE DATABASE IF NOT EXISTS dataagent DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

USE dataagent;

CREATE TABLE IF NOT EXISTS app_meta (
    meta_key    VARCHAR(64) PRIMARY KEY,
    meta_value  TEXT NOT NULL,
    updated_at  DATETIME NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS semantic_schema_items (
    id             INT PRIMARY KEY AUTO_INCREMENT,
    table_name     VARCHAR(64) NOT NULL,
    column_name    VARCHAR(64) NOT NULL,
    description    VARCHAR(512) NOT NULL,
    synonyms       VARCHAR(512) NOT NULL DEFAULT '',
    join_hint      VARCHAR(512) NOT NULL DEFAULT '',
    schema_version VARCHAR(32) NOT NULL,
    enabled        TINYINT NOT NULL DEFAULT 1,
    is_entity      TINYINT NOT NULL DEFAULT 0,
    updated_at     DATETIME NOT NULL,
    UNIQUE KEY uk_schema_item (table_name, column_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS semantic_tables (
    id             INT PRIMARY KEY AUTO_INCREMENT,
    table_name     VARCHAR(64) NOT NULL,
    summary        VARCHAR(512) NOT NULL,
    schema_version VARCHAR(32) NOT NULL,
    enabled        TINYINT NOT NULL DEFAULT 1,
    updated_at     DATETIME NOT NULL,
    UNIQUE KEY uk_semantic_table (table_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS semantic_metrics (
    id              INT PRIMARY KEY AUTO_INCREMENT,
    name            VARCHAR(64) NOT NULL,
    aliases         VARCHAR(256) NOT NULL DEFAULT '',
    definition      VARCHAR(512) NOT NULL,
    formula         VARCHAR(512) NOT NULL,
    related_tables  VARCHAR(256) NOT NULL DEFAULT '',
    enabled         TINYINT NOT NULL DEFAULT 1,
    updated_at      DATETIME NOT NULL,
    UNIQUE KEY uk_metric_name (name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS conversations (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    session_id  VARCHAR(64) NOT NULL,
    created_at  DATETIME NOT NULL,
    UNIQUE KEY uk_conversations_session (session_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS messages (
    id               BIGINT PRIMARY KEY AUTO_INCREMENT,
    session_id       VARCHAR(64) NOT NULL,
    role             VARCHAR(16) NOT NULL,
    content          TEXT NOT NULL,
    rewritten_question VARCHAR(512) NULL,
    sql_text         TEXT NULL,
    created_at       DATETIME NOT NULL,
    KEY idx_messages_session (session_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS query_logs (
    id                 BIGINT PRIMARY KEY AUTO_INCREMENT,
    session_id         VARCHAR(64) NOT NULL,
    question           VARCHAR(512) NOT NULL,
    rewritten_question VARCHAR(512) NULL,
    sql_text           TEXT NULL,
    status             VARCHAR(32) NOT NULL,
    retry_count        INT NOT NULL DEFAULT 0,
    duration_ms        INT NOT NULL DEFAULT 0,
    row_count          INT NOT NULL DEFAULT 0,
    model_name         VARCHAR(64) NOT NULL DEFAULT '',
    prompt_tokens      INT NOT NULL DEFAULT 0,
    completion_tokens  INT NOT NULL DEFAULT 0,
    error_message      VARCHAR(512) NOT NULL DEFAULT '',
    summary            TEXT NULL,
    summary_source     VARCHAR(16) NOT NULL DEFAULT '',
    warnings           JSON NULL,
    source             VARCHAR(16) NOT NULL DEFAULT 'web',
    created_at         DATETIME NOT NULL,
    KEY idx_logs_created (created_at),
    KEY idx_logs_status (status)
) ENGINE=InnoDB;

-- One row per SQL a question tried: ok, blocked by the checks, or failed in the database.
-- rows_preview holds at most 20 rows of the safe views. It is kept as long as its
-- query_logs row; nothing removes it automatically. To clear old ones:
--   DELETE FROM query_executions WHERE created_at < NOW() - INTERVAL 90 DAY;
CREATE TABLE IF NOT EXISTS query_executions (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    log_id        BIGINT NOT NULL,
    seq           INT NOT NULL,
    query_no      INT NULL,
    purpose       VARCHAR(255) NOT NULL DEFAULT '',
    sql_text      TEXT NOT NULL,
    sql_hash      CHAR(16) NOT NULL DEFAULT '',
    status        VARCHAR(16) NOT NULL,
    row_count     INT NOT NULL DEFAULT 0,
    error         VARCHAR(512) NOT NULL DEFAULT '',
    duration_ms   INT NOT NULL DEFAULT 0,
    rows_preview  JSON NULL,
    created_at    DATETIME NOT NULL,
    KEY idx_query_executions_log (log_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS feedback (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    log_id        BIGINT NOT NULL,
    verdict       VARCHAR(16) NOT NULL,
    created_at    DATETIME NOT NULL,
    UNIQUE KEY uk_feedback_log (log_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS qa_cases (
    id              INT PRIMARY KEY AUTO_INCREMENT,
    question        VARCHAR(512) NOT NULL,
    sql_text        TEXT NOT NULL,
    result_summary  VARCHAR(512) NOT NULL DEFAULT '',
    status          VARCHAR(16) NOT NULL DEFAULT 'pending',
    source_log_id   BIGINT NULL,
    created_at      DATETIME NOT NULL,
    reviewed_at     DATETIME NULL,
    KEY idx_qa_cases_status (status)
) ENGINE=InnoDB;

-- One row per call of a question in query_logs: model, tool, semantic retrieval,
-- embedding, rerank or SQL. error_type is timeout, 429, 5xx or other for a failed call;
-- input_hash is a hash of an embedding, rerank or SQL input, to count repeated calls.
CREATE TABLE IF NOT EXISTS call_traces (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    log_id        BIGINT NOT NULL,
    seq           INT NOT NULL,
    agent         VARCHAR(64) NOT NULL,
    kind          VARCHAR(16) NOT NULL,
    name          VARCHAR(64) NOT NULL,
    started_ms    INT NOT NULL,
    duration_ms   INT NOT NULL,
    input_tokens  INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    ok            TINYINT(1) NOT NULL DEFAULT 1,
    detail        VARCHAR(255) NOT NULL DEFAULT '',
    error_type    VARCHAR(16) NOT NULL DEFAULT '',
    input_hash    CHAR(16) NOT NULL DEFAULT '',
    created_at    DATETIME NOT NULL,
    KEY idx_call_traces_log (log_id)
) ENGINE=InnoDB;

-- Evaluation runs (python -m app.evaluation) and one row per case and attempt.
-- The evaluation account writes these two tables and only reads the others.
CREATE TABLE IF NOT EXISTS eval_runs (
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
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS eval_case_results (
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
) ENGINE=InnoDB;
