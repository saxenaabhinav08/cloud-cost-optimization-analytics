-- Phase 2A additive schema for optional AWS cost-ingestion code.
-- This migration only creates new tables. It never changes billing_records.
-- Apply to an explicitly selected database after taking the normal DB backup.

CREATE TABLE IF NOT EXISTS aws_billing_records (
    record_key CHAR(64) NOT NULL,
    aws_account_id CHAR(12) NOT NULL,
    usage_date DATE NOT NULL,
    service VARCHAR(128) NOT NULL,
    region VARCHAR(64) NOT NULL,
    usage_type VARCHAR(255) NOT NULL,
    line_item_type VARCHAR(128) NOT NULL,
    cost DECIMAL(24,8) NOT NULL,
    currency CHAR(3) NOT NULL,
    cost_metric VARCHAR(32) NOT NULL,
    is_estimated BOOLEAN NOT NULL,
    tags_json JSON NOT NULL,
    source VARCHAR(24) NOT NULL,
    source_version VARCHAR(255) NOT NULL,
    ingested_at DATETIME NOT NULL,
    ingestion_run_id CHAR(36) NOT NULL,
    PRIMARY KEY (record_key),
    KEY ix_aws_billing_source_date (source, usage_date),
    KEY ix_aws_billing_service_date (service, usage_date),
    KEY ix_aws_billing_account_date (aws_account_id, usage_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS aws_billing_revisions (
    id INT NOT NULL AUTO_INCREMENT,
    record_key CHAR(64) NOT NULL,
    action VARCHAR(16) NOT NULL,
    previous_cost DECIMAL(24,8) NOT NULL,
    new_cost DECIMAL(24,8) NULL,
    currency CHAR(3) NOT NULL,
    source_version VARCHAR(255) NOT NULL,
    ingestion_run_id CHAR(36) NOT NULL,
    revised_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY ix_aws_billing_revisions_key (record_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS aws_ingestion_watermarks (
    source VARCHAR(24) NOT NULL,
    scope VARCHAR(16) NOT NULL,
    source_version VARCHAR(255) NOT NULL,
    last_successful_run_id CHAR(36) NOT NULL,
    completed_at DATETIME NOT NULL,
    PRIMARY KEY (source, scope)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS aws_ingestion_runs (
    run_id CHAR(36) NOT NULL,
    source VARCHAR(24) NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    status VARCHAR(16) NOT NULL,
    rows_read INT NOT NULL DEFAULT 0,
    rows_inserted INT NOT NULL DEFAULT 0,
    rows_updated INT NOT NULL DEFAULT 0,
    rows_unchanged INT NOT NULL DEFAULT 0,
    rows_rejected INT NOT NULL DEFAULT 0,
    rows_deleted INT NOT NULL DEFAULT 0,
    source_version VARCHAR(255) NOT NULL DEFAULT '',
    request_count INT NOT NULL DEFAULT 0,
    bytes_scanned BIGINT NOT NULL DEFAULT 0,
    error_summary TEXT NULL,
    PRIMARY KEY (run_id),
    KEY ix_aws_ingestion_runs_source_started (source, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS aws_billing_staging (
    run_id CHAR(36) NOT NULL,
    source VARCHAR(24) NOT NULL,
    scope VARCHAR(16) NOT NULL,
    record_key CHAR(64) NOT NULL,
    cost DECIMAL(24,8) NOT NULL,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, source, scope, record_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Rollback note: because this migration is additive, rollback is limited to
-- dropping these five new aws_* tables after confirming they contain no data
-- needed for audit or recovery. Drop in this order: aws_billing_staging,
-- aws_ingestion_watermarks, aws_ingestion_runs, aws_billing_revisions,
-- aws_billing_records. Never drop the existing billing_records table.
