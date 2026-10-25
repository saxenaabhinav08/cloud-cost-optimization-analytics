"""Transactional repository for canonical AWS billing rows and audit history."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Iterable

from sqlalchemy import (
    Boolean, CHAR, Column, Date, DateTime, Index, Integer, JSON, MetaData, Numeric,
    String, Table, Text, create_engine, delete, insert, select, update,
)
from sqlalchemy.engine import Engine

from .schema import CanonicalBillingRecord

aws_metadata = MetaData()
AWS_BILLING_RECORDS = Table(
    "aws_billing_records", aws_metadata,
    Column("record_key", CHAR(64), primary_key=True),
    Column("aws_account_id", CHAR(12), nullable=False), Column("usage_date", Date, nullable=False),
    Column("service", String(128), nullable=False), Column("region", String(64), nullable=False),
    Column("usage_type", String(255), nullable=False), Column("line_item_type", String(128), nullable=False),
    Column("cost", Numeric(24, 8), nullable=False), Column("currency", CHAR(3), nullable=False),
    Column("cost_metric", String(32), nullable=False), Column("is_estimated", Boolean, nullable=False),
    Column("tags_json", JSON, nullable=False), Column("source", String(24), nullable=False),
    Column("source_version", String(255), nullable=False), Column("ingested_at", DateTime, nullable=False),
    Column("ingestion_run_id", CHAR(36), nullable=False),
)
Index("ix_aws_billing_source_date", AWS_BILLING_RECORDS.c.source, AWS_BILLING_RECORDS.c.usage_date)
Index("ix_aws_billing_service_date", AWS_BILLING_RECORDS.c.service, AWS_BILLING_RECORDS.c.usage_date)
Index("ix_aws_billing_account_date", AWS_BILLING_RECORDS.c.aws_account_id, AWS_BILLING_RECORDS.c.usage_date)

AWS_BILLING_REVISIONS = Table(
    "aws_billing_revisions", aws_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("record_key", CHAR(64), nullable=False),
    Column("action", String(16), nullable=False), Column("previous_cost", Numeric(24, 8), nullable=False),
    Column("new_cost", Numeric(24, 8), nullable=True), Column("currency", CHAR(3), nullable=False),
    Column("source_version", String(255), nullable=False), Column("ingestion_run_id", CHAR(36), nullable=False),
    Column("revised_at", DateTime, nullable=False),
)
Index("ix_aws_billing_revisions_key", AWS_BILLING_REVISIONS.c.record_key)

AWS_INGESTION_WATERMARKS = Table(
    "aws_ingestion_watermarks", aws_metadata,
    Column("source", String(24), primary_key=True), Column("scope", String(16), primary_key=True),
    Column("source_version", String(255), nullable=False), Column("last_successful_run_id", CHAR(36), nullable=False),
    Column("completed_at", DateTime, nullable=False),
)

AWS_INGESTION_RUNS = Table(
    "aws_ingestion_runs", aws_metadata,
    Column("run_id", CHAR(36), primary_key=True), Column("source", String(24), nullable=False),
    Column("started_at", DateTime, nullable=False), Column("finished_at", DateTime, nullable=True),
    Column("status", String(16), nullable=False), Column("rows_read", Integer, nullable=False, default=0),
    Column("rows_inserted", Integer, nullable=False, default=0), Column("rows_updated", Integer, nullable=False, default=0),
    Column("rows_unchanged", Integer, nullable=False, default=0), Column("rows_rejected", Integer, nullable=False, default=0),
    Column("rows_deleted", Integer, nullable=False, default=0), Column("source_version", String(255), nullable=False, default=""),
    Column("request_count", Integer, nullable=False, default=0), Column("bytes_scanned", Integer, nullable=False, default=0),
    Column("error_summary", Text, nullable=True),
)
Index("ix_aws_ingestion_runs_source_started", AWS_INGESTION_RUNS.c.source, AWS_INGESTION_RUNS.c.started_at)

AWS_BILLING_STAGING = Table(
    "aws_billing_staging", aws_metadata,
    Column("run_id", CHAR(36), primary_key=True), Column("source", String(24), primary_key=True),
    Column("scope", String(16), primary_key=True), Column("record_key", CHAR(64), primary_key=True),
    Column("cost", Numeric(24, 8), nullable=False), Column("payload_json", JSON, nullable=False),
)


@dataclass(frozen=True)
class WriteCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0

    def __add__(self, other: "WriteCounts") -> "WriteCounts":
        return WriteCounts(self.inserted + other.inserted, self.updated + other.updated,
                           self.unchanged + other.unchanged, self.deleted + other.deleted)


def create_aws_schema(engine: Engine) -> None:
    """Create only the additive AWS ingestion tables (useful for local tests)."""
    aws_metadata.create_all(engine)


def _naive_utc(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)


def _period_bounds(scope: str) -> tuple[date, date]:
    try:
        month = datetime.strptime(scope, "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError):
        raise ValueError("scope must use YYYY-MM format") from None
    next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return month, next_month


class AwsRepository:
    """Store restatable source scopes atomically and expose ingestion history."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @classmethod
    def sqlite_memory(cls) -> "AwsRepository":
        """Create an isolated SQLite repository for deterministic unit tests."""
        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        create_aws_schema(engine)
        return cls(engine)

    def begin_run(self, run_id: str, source: str, *, started_at: datetime | None = None) -> None:
        with self.engine.begin() as connection:
            connection.execute(insert(AWS_INGESTION_RUNS).values(
                run_id=run_id, source=source, started_at=_naive_utc(started_at), status="running",
                rows_read=0, rows_inserted=0, rows_updated=0, rows_unchanged=0, rows_rejected=0,
                rows_deleted=0, source_version="", request_count=0, bytes_scanned=0,
            ))

    def finish_run(self, run_id: str, *, status: str, rows_read: int = 0, counts: WriteCounts = WriteCounts(),
                   rows_rejected: int = 0, source_version: str = "", request_count: int = 0,
                   bytes_scanned: int = 0, error_summary: str | None = None,
                   finished_at: datetime | None = None) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("status must be succeeded or failed")
        with self.engine.begin() as connection:
            connection.execute(update(AWS_INGESTION_RUNS).where(AWS_INGESTION_RUNS.c.run_id == run_id).values(
                finished_at=_naive_utc(finished_at), status=status, rows_read=rows_read,
                rows_inserted=counts.inserted, rows_updated=counts.updated, rows_unchanged=counts.unchanged,
                rows_rejected=rows_rejected, rows_deleted=counts.deleted, source_version=source_version,
                request_count=request_count, bytes_scanned=bytes_scanned, error_summary=error_summary,
            ))

    def watermark(self, source: str, scope: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(AWS_INGESTION_WATERMARKS).where(
                AWS_INGESTION_WATERMARKS.c.source == source,
                AWS_INGESTION_WATERMARKS.c.scope == scope,
            )).mappings().first()
        return dict(row) if row else None

    def watermarks_for_source(self, source: str) -> list[dict[str, object]]:
        """List stored source scopes and versions for incremental planning."""
        with self.engine.connect() as connection:
            rows = connection.execute(select(AWS_INGESTION_WATERMARKS).where(
                AWS_INGESTION_WATERMARKS.c.source == source,
            )).mappings().all()
        return [dict(row) for row in rows]

    def revisions_for_key(self, record_key: str) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(AWS_BILLING_REVISIONS).where(
                AWS_BILLING_REVISIONS.c.record_key == record_key,
            ).order_by(AWS_BILLING_REVISIONS.c.id)).mappings().all()
        return [dict(row) for row in rows]

    def run_row(self, run_id: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(AWS_INGESTION_RUNS).where(
                AWS_INGESTION_RUNS.c.run_id == run_id,
            )).mappings().first()
        return dict(row) if row else None

    def _write_record(self, connection, record: CanonicalBillingRecord, run_id: str) -> str:
        existing = connection.execute(select(AWS_BILLING_RECORDS).where(
            AWS_BILLING_RECORDS.c.record_key == record.record_key,
        )).mappings().first()
        row = {
            "record_key": record.record_key, "aws_account_id": record.aws_account_id,
            "usage_date": record.usage_date, "service": record.service, "region": record.region,
            "usage_type": record.usage_type, "line_item_type": record.line_item_type,
            "cost": record.cost, "currency": record.currency, "cost_metric": record.cost_metric,
            "is_estimated": record.is_estimated, "tags_json": dict(record.tags), "source": record.source,
            "source_version": record.source_version, "ingested_at": _naive_utc(record.ingested_at),
            "ingestion_run_id": run_id,
        }
        if existing is None:
            connection.execute(insert(AWS_BILLING_RECORDS).values(**row))
            return "inserted"
        previous = Decimal(str(existing["cost"]))
        if previous != record.cost:
            connection.execute(insert(AWS_BILLING_REVISIONS).values(
                record_key=record.record_key, action="updated", previous_cost=previous,
                new_cost=record.cost, currency=record.currency, source_version=record.source_version,
                ingestion_run_id=run_id, revised_at=_naive_utc(),
            ))
            connection.execute(update(AWS_BILLING_RECORDS).where(
                AWS_BILLING_RECORDS.c.record_key == record.record_key,
            ).values(**row))
            return "updated"
        # Keep exact repeat imports write-free; a changed source version is metadata,
        # so update only that field while preserving no-op cost/revision semantics.
        if existing["source_version"] != record.source_version:
            connection.execute(update(AWS_BILLING_RECORDS).where(
                AWS_BILLING_RECORDS.c.record_key == record.record_key,
            ).values(source_version=record.source_version, is_estimated=record.is_estimated))
        return "unchanged"

    def replace_scope(self, source: str, scope: str, records: Iterable[CanonicalBillingRecord], *,
                      run_id: str, source_version: str = "", fail_after: int | None = None) -> WriteCounts:
        """Replace one full source/billing month within a single transaction.

        An exception at any point rolls back inserts, updates, revision rows and
        deletes for this scope. Watermarks are intentionally advanced separately
        only after the orchestrator finishes every scope successfully.
        """
        start, end = _period_bounds(scope)
        incoming = list(records)
        if any(record.source != source or not start <= record.usage_date < end for record in incoming):
            raise ValueError("all records must match the selected source and billing-month scope")
        keys = {record.record_key for record in incoming}
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        with self.engine.begin() as connection:
            staging_rows = [{
                "run_id": run_id, "source": source, "scope": scope, "record_key": record.record_key,
                "cost": record.cost,
                "payload_json": {
                    "aws_account_id": record.aws_account_id, "usage_date": record.usage_date.isoformat(),
                    "service": record.service, "region": record.region, "usage_type": record.usage_type,
                    "line_item_type": record.line_item_type, "currency": record.currency,
                    "cost_metric": record.cost_metric, "is_estimated": record.is_estimated,
                    "tags": dict(record.tags), "source_version": record.source_version,
                },
            } for record in incoming]
            if staging_rows:
                connection.execute(insert(AWS_BILLING_STAGING), staging_rows)
            staged_keys = set(connection.execute(select(AWS_BILLING_STAGING.c.record_key).where(
                AWS_BILLING_STAGING.c.run_id == run_id, AWS_BILLING_STAGING.c.source == source,
                AWS_BILLING_STAGING.c.scope == scope,
            )).scalars())
            if staged_keys != keys:
                raise RuntimeError("staged scope keys do not match the replacement batch")
            existing_rows = connection.execute(select(AWS_BILLING_RECORDS).where(
                AWS_BILLING_RECORDS.c.source == source,
                AWS_BILLING_RECORDS.c.usage_date >= start,
                AWS_BILLING_RECORDS.c.usage_date < end,
            )).mappings().all()
            existing_keys = {row["record_key"] for row in existing_rows}
            for index, record in enumerate(incoming, start=1):
                action = self._write_record(connection, record, run_id)
                counts[action] += 1
                if fail_after is not None and index >= fail_after:
                    raise RuntimeError("injected transactional failure")
            missing = existing_keys - keys
            for old in existing_rows:
                if old["record_key"] in missing:
                    connection.execute(insert(AWS_BILLING_REVISIONS).values(
                        record_key=old["record_key"], action="deleted", previous_cost=old["cost"], new_cost=None,
                        currency=old["currency"], source_version=source_version, ingestion_run_id=run_id,
                        revised_at=_naive_utc(),
                    ))
            if missing:
                connection.execute(delete(AWS_BILLING_RECORDS).where(AWS_BILLING_RECORDS.c.record_key.in_(missing)))
                counts["deleted"] = len(missing)
            connection.execute(delete(AWS_BILLING_STAGING).where(
                AWS_BILLING_STAGING.c.run_id == run_id, AWS_BILLING_STAGING.c.source == source,
                AWS_BILLING_STAGING.c.scope == scope,
            ))
        return WriteCounts(**counts)

    def advance_watermarks(self, source: str, scopes: Iterable[str], *, run_id: str,
                           source_version: str = "", completed_at: datetime | None = None) -> None:
        """Advance successful source/scope watermarks after all scope writes commit."""
        now = _naive_utc(completed_at)
        with self.engine.begin() as connection:
            for scope in sorted(set(scopes)):
                existing = connection.execute(select(AWS_INGESTION_WATERMARKS).where(
                    AWS_INGESTION_WATERMARKS.c.source == source,
                    AWS_INGESTION_WATERMARKS.c.scope == scope,
                )).first()
                values = {"source_version": source_version, "last_successful_run_id": run_id, "completed_at": now}
                if existing:
                    connection.execute(update(AWS_INGESTION_WATERMARKS).where(
                        AWS_INGESTION_WATERMARKS.c.source == source,
                        AWS_INGESTION_WATERMARKS.c.scope == scope,
                    ).values(**values))
                else:
                    connection.execute(insert(AWS_INGESTION_WATERMARKS).values(
                        source=source, scope=scope, **values,
                    ))

    def rows_for_scope(self, source: str, scope: str) -> list[dict[str, object]]:
        start, end = _period_bounds(scope)
        with self.engine.connect() as connection:
            rows = connection.execute(select(AWS_BILLING_RECORDS).where(
                AWS_BILLING_RECORDS.c.source == source,
                AWS_BILLING_RECORDS.c.usage_date >= start,
                AWS_BILLING_RECORDS.c.usage_date < end,
            ).order_by(AWS_BILLING_RECORDS.c.usage_date, AWS_BILLING_RECORDS.c.service)).mappings().all()
        result = [dict(row) for row in rows]
        for row in result:
            row["tags"] = row.pop("tags_json")
        return result
