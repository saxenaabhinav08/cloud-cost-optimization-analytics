"""Optional SQLAlchemy configuration and schema mapping for local MySQL 8."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    func,
)
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.dialects.mysql import BIGINT, CHAR, insert as mysql_insert
from sqlalchemy import create_engine


PROJECT_ROOT = Path(__file__).resolve().parents[2]
metadata = MetaData()

BILLING_RECORDS = Table(
    "billing_records",
    metadata,
    Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
    Column("billing_month", Date, nullable=False),
    Column("account_id", String(32), nullable=False),
    Column("region", String(64), nullable=False),
    Column("service", String(128), nullable=False),
    Column("usage_type", String(128), nullable=False),
    Column("cost_usd", Numeric(12, 2), nullable=False),
    Column("is_fictional", Boolean, nullable=False),
    Column("dataset_note", String(255), nullable=False),
    Column("record_fingerprint", CHAR(64), nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    CheckConstraint("cost_usd >= 0", name="chk_billing_records_nonnegative_cost"),
)
Index("ix_billing_records_month", BILLING_RECORDS.c.billing_month)
Index(
    "ix_billing_records_service_month",
    BILLING_RECORDS.c.service,
    BILLING_RECORDS.c.billing_month,
)
Index(
    "ix_billing_records_region_month",
    BILLING_RECORDS.c.region,
    BILLING_RECORDS.c.billing_month,
)
Index(
    "ix_billing_records_account_month",
    BILLING_RECORDS.c.account_id,
    BILLING_RECORDS.c.billing_month,
)


def database_url_from_env(environ: Mapping[str, str] | None = None) -> URL:
    """Build a safely escaped MySQL URL from explicit local environment settings."""
    values = os.environ if environ is None else environ
    raw_url = values.get("DATABASE_URL")
    if raw_url:
        url = make_url(raw_url)
        if url.drivername != "mysql+pymysql":
            raise ValueError("DATABASE_URL must use the mysql+pymysql driver")
        return url

    required = ("DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise ValueError(
            "Missing MySQL setting(s): " + ", ".join(missing)
            + ". Copy .env.example to .env and fill in local database settings."
        )
    try:
        port = int(values.get("DATABASE_PORT", "3306"))
    except ValueError as exc:
        raise ValueError("DATABASE_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("DATABASE_PORT must be between 1 and 65535")

    return URL.create(
        "mysql+pymysql",
        username=values["DATABASE_USER"],
        password=values["DATABASE_PASSWORD"],
        host=values.get("DATABASE_HOST", "localhost"),
        port=port,
        database=values["DATABASE_NAME"],
        query={"charset": "utf8mb4"},
    )


def create_db_engine(environ: Mapping[str, str] | None = None) -> Engine:
    """Create a MySQL engine; this does not connect until it is first used."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return create_engine(
        database_url_from_env(environ),
        pool_pre_ping=True,
        echo=False,
    )


def mysql_insert_statement():
    """Build a plain insert; duplicate handling stays in the importer savepoint."""
    return mysql_insert(BILLING_RECORDS)
