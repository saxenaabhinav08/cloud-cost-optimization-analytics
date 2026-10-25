"""Shared data preparation helpers for the Streamlit dashboard."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

from .aggregation import aggregate_by_service_month
from .anomalies import detect_spending_anomalies
from .database import PROJECT_ROOT, create_db_engine
from .validation import BillingRecord, load_billing_data

SAMPLE_CSV = PROJECT_ROOT / "data" / "raw" / "fictional_aws_billing.csv"


@dataclass(frozen=True)
class DashboardData:
    monthly_costs: list[dict[str, str]]
    region_costs: list[dict[str, str]]
    anomalies: list[dict[str, str | None]]
    record_count: int


@dataclass(frozen=True)
class SpendingSummary:
    total_spend: Decimal
    latest_month: str | None
    latest_month_spend: Decimal | None
    previous_month: str | None
    month_over_month_change: Decimal | None
    month_over_month_percent: Decimal | None


def load_sample_records(path: str | Path = SAMPLE_CSV) -> list[BillingRecord]:
    """Load and validate the included fictional development dataset."""
    return load_billing_data(path)


def load_mysql_records() -> list[BillingRecord]:
    """Read only fictional records from the configured local MySQL database."""
    query = text("""
        SELECT billing_month, account_id, region, service, usage_type,
               cost_usd, is_fictional, dataset_note
        FROM billing_records
        WHERE is_fictional = TRUE
        ORDER BY billing_month, service, region, account_id
    """)
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return [
            BillingRecord(
                billing_month=row["billing_month"].strftime("%Y-%m"),
                account_id=row["account_id"], region=row["region"],
                service=row["service"], usage_type=row["usage_type"],
                cost_usd=Decimal(str(row["cost_usd"])),
                is_fictional=bool(row["is_fictional"]), dataset_note=row["dataset_note"],
            )
            for row in rows
        ]
    finally:
        engine.dispose()


def build_dashboard_data(records: list[BillingRecord]) -> DashboardData:
    """Use project aggregation and anomaly logic to prepare display rows."""
    monthly = aggregate_by_service_month(records)
    anomalies: list[dict[str, str | None]] = []
    for item in detect_spending_anomalies(monthly):
        baseline = Decimal(item["baseline_cost_usd"])
        percentage = (
            f"{Decimal(item['increase_usd']) / baseline * Decimal('100'):.2f}"
            if baseline else None
        )
        anomalies.append({**item, "percentage_change": percentage})

    by_region: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for record in records:
        by_region[(record.billing_month, record.region)] += record.cost_usd
    regions = [
        {"billing_month": month, "region": region, "total_cost_usd": f"{amount:.2f}"}
        for (month, region), amount in sorted(by_region.items())
    ]
    return DashboardData(monthly, regions, anomalies, len(records))


def _in_months(rows: Iterable[dict[str, str]], months: set[str] | None) -> list[dict[str, str]]:
    return [row for row in rows if months is None or row["billing_month"] in months]


def monthly_totals(monthly_costs: list[dict[str, str]], months: set[str] | None = None) -> list[dict[str, str]]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in _in_months(monthly_costs, months):
        totals[row["billing_month"]] += Decimal(row["total_cost_usd"])
    return [{"billing_month": month, "total_cost_usd": f"{amount:.2f}"} for month, amount in sorted(totals.items())]


def service_totals(monthly_costs: list[dict[str, str]], months: set[str] | None = None) -> list[dict[str, str]]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in _in_months(monthly_costs, months):
        totals[row["service"]] += Decimal(row["total_cost_usd"])
    return [{"service": name, "total_cost_usd": f"{amount:.2f}"} for name, amount in sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))]


def region_totals(region_costs: list[dict[str, str]], months: set[str] | None = None) -> list[dict[str, str]]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in _in_months(region_costs, months):
        totals[row["region"]] += Decimal(row["total_cost_usd"])
    return [{"region": name, "total_cost_usd": f"{amount:.2f}"} for name, amount in sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))]


def summarize_spending(monthly_costs: list[dict[str, str]], months: set[str] | None = None) -> SpendingSummary:
    totals = monthly_totals(monthly_costs, months)
    if not totals:
        return SpendingSummary(Decimal("0.00"), None, None, None, None, None)
    values = [(row["billing_month"], Decimal(row["total_cost_usd"])) for row in totals]
    latest_month, latest = values[-1]
    if len(values) < 2:
        return SpendingSummary(latest, latest_month, latest, None, None, None)
    previous_month, previous = values[-2]
    change = latest - previous
    percent = change / previous * Decimal("100") if previous else None
    return SpendingSummary(sum((value for _, value in values), Decimal("0")), latest_month, latest, previous_month, change, percent)
