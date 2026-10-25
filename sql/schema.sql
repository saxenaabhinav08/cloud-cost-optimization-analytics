-- MySQL 8 schema for explicitly fictional/development billing records.
-- Select or create the database separately before running this file.
-- This DDL is additive: it creates the table only when it does not exist.
CREATE TABLE IF NOT EXISTS billing_records (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    billing_month DATE NOT NULL COMMENT 'First day of the billing month',
    account_id VARCHAR(32) NOT NULL COMMENT 'Fictional account identifier in this sample',
    region VARCHAR(64) NOT NULL,
    service VARCHAR(128) NOT NULL,
    usage_type VARCHAR(128) NOT NULL,
    cost_usd DECIMAL(12, 2) NOT NULL,
    is_fictional BOOLEAN NOT NULL,
    dataset_note VARCHAR(255) NOT NULL,
    record_fingerprint CHAR(64) NOT NULL COMMENT 'SHA-256 of normalized source record fields; used for idempotency',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT uq_billing_records_fingerprint UNIQUE (record_fingerprint),
    CONSTRAINT chk_billing_records_nonnegative_cost CHECK (cost_usd >= 0),
    KEY ix_billing_records_month (billing_month),
    KEY ix_billing_records_service_month (service, billing_month),
    KEY ix_billing_records_region_month (region, billing_month),
    KEY ix_billing_records_account_month (account_id, billing_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
