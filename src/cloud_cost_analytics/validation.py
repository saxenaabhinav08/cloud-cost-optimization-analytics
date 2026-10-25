"""Input loading and validation for the fictional billing CSV."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


REQUIRED_COLUMNS = {
    "billing_month",
    "account_id",
    "region",
    "service",
    "usage_type",
    "cost_usd",
    "is_fictional",
    "dataset_note",
}


@dataclass(frozen=True)
class BillingRecord:
    billing_month: str
    account_id: str
    region: str
    service: str
    usage_type: str
    cost_usd: Decimal
    is_fictional: bool
    dataset_note: str


class DataValidationError(ValueError):
    """Raised when a billing file is missing or contains invalid fields."""


def load_billing_data(path: str | Path) -> list[BillingRecord]:
    """Load records and reject malformed, negative, or non-fictional input."""
    file_path = Path(path)
    if not file_path.is_file():
        raise DataValidationError(f"Input file does not exist: {file_path}")

    records: list[BillingRecord] = []
    with file_path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise DataValidationError(
                "Missing required column(s): " + ", ".join(sorted(missing))
            )

        for line_number, row in enumerate(reader, start=2):
            label = f"line {line_number}"
            for name in REQUIRED_COLUMNS - {"cost_usd", "is_fictional"}:
                if not (row.get(name) or "").strip():
                    raise DataValidationError(f"{label}: {name} must not be blank")
            month = row["billing_month"].strip()
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                raise DataValidationError(
                    f"{label}: billing_month must use YYYY-MM format"
                )
            try:
                datetime.strptime(month, "%Y-%m")
            except ValueError as exc:
                raise DataValidationError(
                    f"{label}: billing_month must use YYYY-MM format"
                ) from exc
            try:
                cost = Decimal(row["cost_usd"].strip())
            except (InvalidOperation, AttributeError) as exc:
                raise DataValidationError(f"{label}: cost_usd must be numeric") from exc
            if not cost.is_finite() or cost < 0:
                raise DataValidationError(f"{label}: cost_usd must be finite and non-negative")

            fictional = row["is_fictional"].strip().lower()
            if fictional != "true":
                raise DataValidationError(
                    f"{label}: this development pipeline accepts fictional data only; "
                    "is_fictional must be true"
                )
            note = row["dataset_note"].strip()
            if "fictional" not in note.lower():
                raise DataValidationError(f"{label}: dataset_note must identify fictional data")

            records.append(
                BillingRecord(
                    billing_month=month,
                    account_id=row["account_id"].strip(),
                    region=row["region"].strip(),
                    service=row["service"].strip(),
                    usage_type=row["usage_type"].strip(),
                    cost_usd=cost,
                    is_fictional=True,
                    dataset_note=note,
                )
            )

    if not records:
        raise DataValidationError("Input file contains no billing records")
    return records
