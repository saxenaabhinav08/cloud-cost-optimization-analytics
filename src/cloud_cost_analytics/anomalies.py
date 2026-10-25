"""A transparent, deliberately simple month-over-month spending rule."""

from decimal import Decimal


def detect_spending_anomalies(
    monthly_costs: list[dict[str, str]], *, multiplier: Decimal = Decimal("1.5"),
    minimum_increase_usd: Decimal = Decimal("100.00"),
) -> list[dict[str, str]]:
    """Flag a service-month if cost exceeds both prior-month thresholds.

    The comparison baseline is the mean of up to the three immediately preceding
    calendar months for that service. A flag requires current cost to be greater
    than 1.5 times that mean *and* at least $100 above it. At least one prior
    month is required. This is a teaching heuristic, not a statistical model;
    flagged months should be reviewed by a person.
    """
    by_service: dict[str, dict[str, Decimal]] = {}
    for row in monthly_costs:
        by_service.setdefault(row["service"], {})[row["billing_month"]] = Decimal(
            row["total_cost_usd"]
        )

    anomalies: list[dict[str, str]] = []
    for service, month_costs in by_service.items():
        for month in sorted(month_costs):
            year, number = (int(part) for part in month.split("-"))
            previous_months = []
            for offset in (1, 2, 3):
                index = year * 12 + (number - 1) - offset
                prior = f"{index // 12:04d}-{index % 12 + 1:02d}"
                if prior in month_costs:
                    previous_months.append(month_costs[prior])
            if not previous_months:
                continue
            baseline = sum(previous_months, Decimal("0")) / len(previous_months)
            current = month_costs[month]
            increase = current - baseline
            if current > baseline * multiplier and increase >= minimum_increase_usd:
                anomalies.append(
                    {
                        "billing_month": month,
                        "service": service,
                        "current_cost_usd": f"{current:.2f}",
                        "baseline_cost_usd": f"{baseline:.2f}",
                        "increase_usd": f"{increase:.2f}",
                        "rule": "current > 1.5x prior up-to-3-month mean and increase >= $100",
                        "review_note": "Fictional-data rule flag; investigate before drawing conclusions.",
                    }
                )
    return sorted(anomalies, key=lambda row: (row["billing_month"], row["service"]))
