"""Idempotently load validated CSV records into an existing MySQL 8 table."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .database import create_db_engine, mysql_insert_statement
from .validation import BillingRecord, load_billing_data


def record_to_row(record: BillingRecord) -> dict[str, object]:
    """Map a validated record and attach its stable exact-record fingerprint."""
    canonical = {
        "billing_month": record.billing_month,
        "account_id": record.account_id,
        "region": record.region,
        "service": record.service,
        "usage_type": record.usage_type,
        "cost_usd": format(record.cost_usd.quantize(Decimal("0.01")), ".2f"),
        "is_fictional": record.is_fictional,
        "dataset_note": record.dataset_note,
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return {
        "billing_month": date.fromisoformat(record.billing_month + "-01"),
        "account_id": record.account_id,
        "region": record.region,
        "service": record.service,
        "usage_type": record.usage_type,
        "cost_usd": record.cost_usd,
        "is_fictional": record.is_fictional,
        "dataset_note": record.dataset_note,
        "record_fingerprint": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def import_records(records: list[BillingRecord], engine: Engine) -> int:
    """Submit records in one transaction; duplicate fingerprints are no-ops.

    Returns the number of validated source rows submitted. Savepoints isolate
    duplicate-key errors so only the duplicate row is skipped.
    """
    if not records:
        return 0
    rows = [record_to_row(record) for record in records]
    statement = mysql_insert_statement()
    with engine.begin() as connection:
        for row in rows:
            try:
                with connection.begin_nested():
                    connection.execute(statement, row)
            except IntegrityError as exc:
                error_args = getattr(exc.orig, "args", ())
                if not error_args or error_args[0] != 1062:
                    raise
    return len(rows)


def import_csv(input_path: str | Path, engine: Engine | None = None) -> int:
    """Validate the complete fictional CSV before opening a database connection."""
    records = load_billing_data(input_path)
    owns_engine = engine is None
    active_engine = engine or create_db_engine()
    try:
        return import_records(records, active_engine)
    finally:
        if owns_engine:
            active_engine.dispose()


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=project_root / "data" / "raw" / "fictional_aws_billing.csv",
        help="Path to the explicitly fictional development CSV.",
    )
    args = parser.parse_args()
    try:
        count = import_csv(args.input)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    except SQLAlchemyError as exc:
        parser.error(
            "MySQL operation failed (" + type(exc).__name__
            + "); error details omitted to protect local credentials."
        )
    print(f"Submitted {count} validated fictional rows; duplicate records are no-ops.")
    print("No AWS connection was made.")


if __name__ == "__main__":
    main()
