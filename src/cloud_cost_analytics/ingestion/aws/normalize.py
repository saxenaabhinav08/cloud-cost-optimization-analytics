"""Quarantine malformed source rows and adapt valid rows to existing analytics."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from collections import defaultdict
from collections.abc import Mapping, Iterable

import pandas as pd

from .config import AwsConfig
from .errors import MalformedRatioExceededError, MixedCurrencyError
from .schema import CanonicalBillingRecord, normalize_tags


@dataclass(frozen=True)
class RejectedRecord:
    index: int
    reason: str


@dataclass(frozen=True)
class NormalizationReport:
    records: tuple[CanonicalBillingRecord, ...]
    rejected: tuple[RejectedRecord, ...]
    counts: Mapping[str, int]
    sample_reasons: tuple[str, ...]
    total_rows: int


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        raise ValueError("invalid_usage_date") from None


def _decimal(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("invalid_cost")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid_cost") from None
    if not amount.is_finite():
        raise ValueError("invalid_cost")
    return amount


def _mapped_tags(raw_tags: object, field_map: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    tags = normalize_tags(raw_tags)
    fields: dict[str, str] = {"team": "", "environment": "", "project": ""}
    normalized_map = {str(key).strip().casefold(): value for key, value in field_map.items()}
    for key, value in tags.items():
        destination = normalized_map.get(key)
        if destination in fields:
            fields[destination] = value
    return tags, fields


def _normalize_one(
    row: Mapping[str, object], *, index: int, source: str, cost_metric: str,
    source_version: str, ingestion_run_id: str, tag_field_map: Mapping[str, str],
    start_date: date | None, end_date: date | None,
) -> tuple[CanonicalBillingRecord, dict[str, str]]:
    from .schema import normalize_account_id, normalize_service

    account_value = row.get("aws_account_id", row.get("account_id"))
    if account_value is None or not str(account_value).strip():
        raise ValueError("missing_account_id")
    account = normalize_account_id(account_value)
    usage_date = _date(row.get("usage_date", row.get("billing_date")))
    if start_date and usage_date < start_date or end_date and usage_date >= end_date:
        raise ValueError("unsupported_date_range")
    service = normalize_service(row.get("service", row.get("product_name", "")))
    cost = _decimal(row.get("cost", row.get("cost_usd")))
    currency = str(row.get("currency", "USD") or "").strip().upper()
    raw_tags = row.get("tags", row.get("resource_tags", {}))
    tags, mapped = _mapped_tags(raw_tags, tag_field_map)
    # Tag fields can also arrive in the flat shape used by some CUR exports.
    for key, target in tag_field_map.items():
        flat = row.get(f"resource_tags_user_{key}", row.get(f"user:{key}"))
        if flat is not None and str(flat).strip():
            tags[str(key).casefold()] = str(flat).strip()
            mapped[target] = str(flat).strip()
    record = CanonicalBillingRecord(
        aws_account_id=account, usage_date=usage_date, service=service,
        region=str(row.get("region") or "global").strip() or "global",
        usage_type=str(row.get("usage_type") or "Aggregated").strip() or "Aggregated",
        line_item_type=str(row.get("line_item_type") or ("Aggregated" if source == "cost_explorer" else "Usage")),
        cost=cost, currency=currency, cost_metric=str(row.get("cost_metric") or cost_metric),
        is_estimated=bool(row.get("is_estimated", row.get("estimated", False))), tags=tags,
        source=source, source_version=str(row.get("source_version") or source_version),
        ingested_at=datetime.now(timezone.utc), ingestion_run_id=ingestion_run_id,
    )
    return record, mapped


def normalize_records(
    rows: Iterable[Mapping[str, object]], *, source: str, cost_metric: str = "UnblendedCost",
    source_version: str = "", ingestion_run_id: str = "", max_malformed_ratio: float = 0.05,
    allow_mixed_currencies: bool = False, tag_field_map: Mapping[str, str] | None = None,
    start_date: date | None = None, end_date: date | None = None,
) -> NormalizationReport:
    """Normalize and aggregate a source batch; malformed rows are quarantined.

    Rejection samples contain only row indices and reason codes, never source
    values, tags or credential-shaped text. Negative credit/refund values remain
    valid Decimal amounts.
    """
    if not 0 <= max_malformed_ratio <= 1:
        raise ValueError("max_malformed_ratio must be between 0 and 1")
    mapping = tag_field_map or {"team": "team", "environment": "environment", "env": "environment", "project": "project"}
    accepted: list[CanonicalBillingRecord] = []
    rejected: list[RejectedRecord] = []
    projected_fields: dict[str, dict[str, str]] = {}
    total = 0
    for index, row in enumerate(rows, start=1):
        total += 1
        try:
            record, fields = _normalize_one(
                row, index=index, source=source, cost_metric=cost_metric,
                source_version=source_version, ingestion_run_id=ingestion_run_id,
                tag_field_map=mapping, start_date=start_date, end_date=end_date,
            )
            accepted.append(record)
            projected_fields[record.record_key] = fields
        except (ValueError, TypeError) as exc:
            reason = str(exc) if str(exc) in {
                "missing_account_id", "invalid_account_id", "invalid_usage_date", "unsupported_date_range",
                "missing_service", "invalid_cost", "invalid_currency", "invalid_tags", "invalid_source",
                "missing_usage_type", "missing_cost_metric",
            } else "invalid_record"
            rejected.append(RejectedRecord(index, reason))

    currencies = {record.currency for record in accepted}
    if len(currencies) > 1 and not allow_mixed_currencies:
        raise MixedCurrencyError("Mixed currencies are not aggregated; normalize one currency per batch.")

    # Aggregate on the canonical grain before persistence. Costs never pass
    # through binary floating-point during this operation.
    sums: dict[str, Decimal] = defaultdict(Decimal)
    first: dict[str, CanonicalBillingRecord] = {}
    for record in accepted:
        sums[record.record_key] += record.cost
        first.setdefault(record.record_key, record)
    aggregated = tuple(replace(first[key], cost=sums[key]) for key in first)
    reasons: dict[str, int] = defaultdict(int)
    for item in rejected:
        reasons[item.reason] += 1
    sample_reasons = tuple(f"row {item.index}: {item.reason}" for item in rejected[:5])
    if total and len(rejected) / total > max_malformed_ratio:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())) or "none"
        raise MalformedRatioExceededError(
            f"Rejected {len(rejected)} of {total} source rows; rejection reasons: {summary}; "
            f"sample rows: {'; '.join(sample_reasons) or 'none'}."
        )
    return NormalizationReport(
        records=aggregated, rejected=tuple(rejected), counts=dict(reasons),
        sample_reasons=sample_reasons, total_rows=total,
    )


def to_analytics_frame(
    records: Iterable[CanonicalBillingRecord],
    *, tag_field_map: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Project canonical rows onto the existing analytics DataFrame columns.

    The canonical record remains Decimal-exact; this adapter intentionally uses
    numeric USD values because the existing pandas analytics entry points use
    numeric series operations and their public column names must stay stable.
    """
    rows = []
    mapping = tag_field_map or {"team": "team", "environment": "environment", "env": "environment", "project": "project"}
    for record in records:
        tags, fields = _mapped_tags(record.tags, mapping)
        rows.append({
            "record_id": record.record_key, "billing_date": pd.Timestamp(record.usage_date),
            "billing_month": record.usage_date.strftime("%Y-%m"), "account_id": record.aws_account_id,
            "region": record.region, "service": record.service, "usage_type": record.usage_type,
            "resource_id": "", "resource_state": "unknown", "cost_usd": float(record.cost),
            "team": fields["team"], "project": fields["project"],
            "environment": fields["environment"], "scenario": "",
            "dataset_note": f"AWS ingestion code-only source · {record.source}",
        })
    columns = ["record_id", "billing_date", "billing_month", "account_id", "region", "service",
               "usage_type", "resource_id", "resource_state", "cost_usd", "team", "project",
               "environment", "scenario", "dataset_note"]
    frame = pd.DataFrame(rows, columns=columns)
    if not frame.empty:
        frame["cost_usd"] = pd.to_numeric(frame["cost_usd"], errors="raise")
    return frame


def normalize_with_config(rows: Iterable[Mapping[str, object]], config: AwsConfig, **kwargs) -> NormalizationReport:
    """Apply the configured malformed-row threshold and tag mapping."""
    return normalize_records(rows, max_malformed_ratio=config.max_malformed_ratio,
                             tag_field_map=config.tag_field_map, **kwargs)
