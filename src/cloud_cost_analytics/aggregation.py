"""Monthly cost aggregation helpers."""

from collections import defaultdict
from decimal import Decimal

from .validation import BillingRecord


def aggregate_by_service_month(
    records: list[BillingRecord],
) -> list[dict[str, str]]:
    """Return one total per service and month, sorted chronologically."""
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for record in records:
        totals[(record.billing_month, record.service)] += record.cost_usd

    return [
        {
            "billing_month": month,
            "service": service,
            "total_cost_usd": f"{amount:.2f}",
        }
        for (month, service), amount in sorted(totals.items())
    ]
