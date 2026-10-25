"""Reproducible, explicitly synthetic daily cloud billing data for the dashboard."""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .database import PROJECT_ROOT


DEFAULT_SEED = 42
DEFAULT_START = date(2025, 1, 1)
DEFAULT_END = date(2025, 12, 31)
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "synthetic" / "daily_cloud_billing.csv"
DATASET_NOTE = "SYNTHETIC GENERATED DATA - not from any AWS account"

ACCOUNTS = ("synthetic-001", "synthetic-002", "synthetic-003")
REGIONS = ("us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1")
SERVICES = (
    "Amazon EC2", "Amazon S3", "Amazon RDS", "Amazon EBS", "NAT Gateway",
    "Data Transfer", "Amazon CloudWatch", "AWS Lambda", "Amazon EKS",
    "Elastic Load Balancing", "Amazon DynamoDB", "Amazon CloudFront",
)

# Per-service daily drift and seasonal profile make month-over-month comparisons
# informative: simulated services can grow, remain nearly flat, or decline.
SERVICE_TRENDS = {
    "Amazon EC2": (0.00095, {4: 1.04, 11: 1.08, 12: 1.10}),
    "Amazon S3": (0.00018, {4: 1.08, 11: 1.03, 12: 1.02}),
    "Amazon RDS": (-0.00016, {4: 1.02, 11: 1.05, 12: 1.01}),
    "Amazon EBS": (0.00034, {4: 1.04, 11: 1.01, 12: 1.05}),
    "NAT Gateway": (0.00125, {4: 1.12, 11: 1.04, 12: 1.02}),
    "Data Transfer": (-0.00022, {4: 1.16, 11: 1.02, 12: 1.06}),
    "Amazon CloudWatch": (0.00066, {4: 1.02, 11: 1.05, 12: 1.08}),
    "AWS Lambda": (0.00012, {4: 1.05, 11: 1.02, 12: 1.03}),
    "Amazon EKS": (0.00105, {4: 1.08, 11: 1.02, 12: 1.04}),
    "Elastic Load Balancing": (-0.00010, {4: 1.03, 11: 1.04, 12: 1.02}),
    "Amazon DynamoDB": (0.00052, {4: 1.04, 11: 1.07, 12: 1.03}),
    "Amazon CloudFront": (-0.00028, {4: 1.14, 11: 1.02, 12: 1.10}),
}

# Each template creates one resource series per account. Several services have
# separate active/idle resources so simulated optimization findings have evidence.
RESOURCE_TEMPLATES = (
    ("Amazon EC2", "Compute hours", "active", "prod", "Platform", "checkout", Decimal("8.40")),
    ("Amazon EC2", "Idle instance hours", "idle", "dev", "Platform", "developer-sandbox", Decimal("2.15")),
    ("Amazon S3", "Storage GB-month", "active", "prod", "Data", "lakehouse", Decimal("4.40")),
    ("Amazon RDS", "Database instance hours", "active", "prod", "Commerce", "orders", Decimal("5.60")),
    ("Amazon RDS", "Low CPU instance hours", "idle", "staging", "Data", "qa-database", Decimal("2.65")),
    ("Amazon EBS", "Provisioned storage GB-month", "active", "prod", "Platform", "checkout", Decimal("1.85")),
    ("Amazon EBS", "Unattached volume GB-month", "unattached", "dev", "Platform", "sandbox-storage", Decimal("0.48")),
    ("NAT Gateway", "NAT gateway hours and processing", "active", "prod", "Platform", "network-core", Decimal("3.10")),
    ("Data Transfer", "Regional and internet transfer", "active", "prod", "Commerce", "checkout", Decimal("1.90")),
    ("Amazon CloudWatch", "Logs ingestion and retention", "active", "prod", "Platform", "observability", Decimal("0.90")),
    ("AWS Lambda", "Function requests and duration", "active", "staging", "Data", "event-pipeline", Decimal("0.58")),
    ("Amazon EKS", "Cluster and worker usage", "active", "prod", "Platform", "checkout", Decimal("3.25")),
    ("Elastic Load Balancing", "Load balancer hours", "active", "prod", "Commerce", "checkout", Decimal("1.05")),
    ("Amazon DynamoDB", "Read/write capacity and storage", "active", "prod", "Data", "catalog", Decimal("0.82")),
    ("Amazon CloudFront", "Data transfer and requests", "active", "prod", "Commerce", "checkout", Decimal("0.74")),
)

SPIKES = {
    "runaway_nat_gateway": {
        "account_id": "synthetic-002", "service": "NAT Gateway",
        "start": date(2025, 8, 12), "days": 9, "multiplier": Decimal("7.0"),
        "cause": "Runaway NAT Gateway processing from a synthetic test batch",
    },
    "data_transfer_burst": {
        "account_id": "synthetic-001", "service": "Data Transfer",
        "start": date(2025, 4, 20), "days": 4, "multiplier": Decimal("5.5"),
        "cause": "Synthetic cross-region data backfill",
    },
    "oversized_dev_cluster": {
        "account_id": "synthetic-003", "service": "Amazon EC2",
        "usage_type": "Compute hours",
        "start": date(2025, 11, 3), "days": 7, "multiplier": Decimal("4.2"),
        "cause": "Synthetic oversized development cluster rollout",
    },
    "unused_rds_capacity": {
        "account_id": "synthetic-002", "service": "Amazon RDS", "usage_type": "Database instance hours",
        "start": date(2025, 3, 13), "days": 5, "multiplier": Decimal("3.6"),
        "cause": "Synthetic oversized RDS test instance left running",
    },
    "log_ingestion_burst": {
        "account_id": "synthetic-001", "service": "Amazon CloudWatch", "usage_type": "Logs ingestion and retention",
        "start": date(2025, 6, 7), "days": 4, "multiplier": Decimal("5.0"),
        "cause": "Synthetic debug logging increased ingestion volume",
    },
    "dynamodb_capacity_spike": {
        "account_id": "synthetic-003", "service": "Amazon DynamoDB", "usage_type": "Read/write capacity and storage",
        "start": date(2025, 7, 22), "days": 6, "multiplier": Decimal("4.8"),
        "cause": "Synthetic provisioned capacity test overshot demand",
    },
    "eks_worker_rollout": {
        "account_id": "synthetic-001", "service": "Amazon EKS", "usage_type": "Cluster and worker usage",
        "start": date(2025, 5, 18), "days": 5, "multiplier": Decimal("3.4"),
        "cause": "Synthetic temporary worker-node rollout",
    },
    "cloudfront_transfer_burst": {
        "account_id": "synthetic-002", "service": "Amazon CloudFront", "usage_type": "Data transfer and requests",
        "start": date(2025, 10, 9), "days": 5, "multiplier": Decimal("4.5"),
        "cause": "Synthetic release generated elevated CDN requests",
    },
}

FIELDS = [
    "record_id", "billing_date", "billing_month", "account_id", "region",
    "service", "usage_type", "resource_id", "resource_state", "cost_usd",
    "team", "project", "environment", "scenario", "dataset_note",
]


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def generate_synthetic_billing(
    *, start: date = DEFAULT_START, end: date = DEFAULT_END, seed: int = DEFAULT_SEED
) -> list[dict[str, str]]:
    """Generate deterministic daily charge records for a fixed or supplied date range."""
    if end < start:
        raise ValueError("end must be on or after start")
    rng = random.Random(seed)
    rows: list[dict[str, str]] = []
    day = start
    ordinal = 0
    while day <= end:
        day_index = (day - start).days
        weekend = Decimal("0.78") if day.weekday() >= 5 else Decimal("1")
        for account_index, account in enumerate(ACCOUNTS):
            for template_index, (service, usage, state, env, team, project, base) in enumerate(RESOURCE_TEMPLATES):
                region = REGIONS[(account_index + template_index) % len(REGIONS)]
                environment_factor = {"prod": Decimal("1.0"), "staging": Decimal("0.64"), "dev": Decimal("0.48")}[env]
                noise = Decimal(str(round(rng.uniform(0.94, 1.06), 4)))
                service_drift, service_seasons = SERVICE_TRENDS[service]
                growth = Decimal("1") + Decimal(day_index) * Decimal(str(service_drift))
                season = Decimal(str(service_seasons.get(day.month, 1.0)))
                amount = base * growth * weekend * season * environment_factor * noise
                scenario = ""
                for scenario_key, event in SPIKES.items():
                    if (
                        account == event["account_id"] and service == event["service"]
                        and usage == event.get("usage_type", usage)
                        and event["start"] <= day < event["start"] + timedelta(days=event["days"])
                    ):
                        amount *= event["multiplier"]
                        scenario = scenario_key
                        break

                # Tags remain blank when missing. No synthetic owner is assigned by fallback.
                team_value = "" if rng.random() < 0.07 else team
                project_value = "" if rng.random() < 0.10 else project
                environment_value = "" if rng.random() < 0.05 else env
                resource_id = f"synth-{account[-3:]}-{service.lower().replace(' ', '-')[:8]}-{state[:4]}"
                record_id = f"synthetic-{day:%Y%m%d}-{account_index + 1:02d}-{template_index + 1:02d}"
                rows.append({
                    "record_id": record_id,
                    "billing_date": day.isoformat(),
                    "billing_month": day.strftime("%Y-%m"),
                    "account_id": account,
                    "region": region,
                    "service": service,
                    "usage_type": usage,
                    "resource_id": resource_id,
                    "resource_state": state,
                    "cost_usd": _money(amount),
                    "team": team_value,
                    "project": project_value,
                    "environment": environment_value,
                    "scenario": scenario,
                    "dataset_note": DATASET_NOTE,
                })
                ordinal += 1
        day += timedelta(days=1)
    return rows


def validate_synthetic_billing(rows: Iterable[dict[str, str]]) -> int:
    """Validate record shape, dates, unique IDs, nonnegative amounts, and provenance."""
    seen_ids: set[str] = set()
    count = 0
    for row_number, row in enumerate(rows, start=1):
        missing = set(FIELDS) - set(row)
        if missing:
            raise ValueError(f"record {row_number}: missing fields {', '.join(sorted(missing))}")
        if row["record_id"] in seen_ids:
            raise ValueError(f"record {row_number}: duplicate record_id")
        seen_ids.add(row["record_id"])
        parsed = date.fromisoformat(row["billing_date"])
        if row["billing_month"] != parsed.strftime("%Y-%m"):
            raise ValueError(f"record {row_number}: billing_month does not match billing_date")
        amount = Decimal(row["cost_usd"])
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"record {row_number}: cost_usd must be finite and nonnegative")
        if "synthetic" not in row["record_id"].lower() or "synthetic" not in row["dataset_note"].lower():
            raise ValueError(f"record {row_number}: synthetic provenance marker is required")
        count += 1
    if count == 0:
        raise ValueError("dataset contains no records")
    return count


def generate_ground_truth(start: date = DEFAULT_START, end: date = DEFAULT_END) -> list[dict[str, str]]:
    """Return one ground-truth row per injected synthetic anomaly day."""
    truth = []
    for scenario, event in SPIKES.items():
        templates = [(i, row) for i, row in enumerate(RESOURCE_TEMPLATES)
                     if row[0] == event["service"] and row[1] == event.get("usage_type", row[1])]
        for template_index, resource_template in templates:
            account_index = ACCOUNTS.index(event["account_id"])
            region = REGIONS[(account_index + template_index) % len(REGIONS)]
            for offset in range(event["days"]):
                event_date = event["start"] + timedelta(days=offset)
                if start <= event_date <= end:
                    truth.append({"billing_date": event_date.isoformat(), "account_id": event["account_id"],
                                  "service": event["service"], "region": region, "scenario": scenario,
                                  "cause": event["cause"], "dataset_note": DATASET_NOTE})
    return truth


def generate_resource_findings(seed: int = DEFAULT_SEED) -> list[dict[str, str]]:
    """Generate separate, explicitly simulated resource optimization evidence."""
    rng = random.Random(seed + 700)
    kinds = [
        ("Idle EC2 instance", "Compute", "Average CPU below 5%", "Stop outside approved operating hours or right-size."),
        ("Underutilized RDS instance", "Database", "Average CPU below 8%", "Review instance class and non-production schedule."),
        ("Unattached EBS volume", "Storage", "Volume has no attached instance", "Snapshot if needed, then review removal."),
        ("Unused Elastic IP", "Networking", "Address is not associated", "Release after owner confirms it is unused."),
        ("Idle load balancer", "Networking", "Very low request count", "Review target health and consolidate if appropriate."),
        ("Old snapshot", "Storage", "Snapshot older than 180 days", "Confirm retention policy and archive candidates."),
        ("S3 lifecycle candidate", "Storage", "Frequently accessed storage retained beyond target", "Review lifecycle transitions and retention requirements."),
    ]
    results = []
    service_by_finding = {
        "Idle EC2 instance": "Amazon EC2", "Underutilized RDS instance": "Amazon RDS",
        "Unattached EBS volume": "Amazon EBS", "Unused Elastic IP": "Amazon EC2",
        "Idle load balancer": "Elastic Load Balancing", "Old snapshot": "Amazon EBS",
        "S3 lifecycle candidate": "Amazon S3",
    }
    team_by_finding = {
        "Idle EC2 instance": "Platform", "Underutilized RDS instance": "Data",
        "Unattached EBS volume": "Platform", "Unused Elastic IP": "Platform",
        "Idle load balancer": "Platform", "Old snapshot": "Data",
        "S3 lifecycle candidate": "Data",
    }
    environment_by_finding = {
        "Idle EC2 instance": "dev", "Underutilized RDS instance": "prod",
        "Unattached EBS volume": "prod", "Unused Elastic IP": "prod",
        "Idle load balancer": "prod", "Old snapshot": "dev",
        "S3 lifecycle candidate": "prod",
    }
    for month in range(1, 13):
        for account_i, account in enumerate(ACCOUNTS):
            for kind_i, (finding, category, evidence, action) in enumerate(kinds):
                cost = round(rng.uniform(12, 160), 2)
                utilization = round(rng.uniform(1, 9), 1) if "CPU" in evidence else ""
                results.append({"billing_month": f"2025-{month:02d}",
                    "resource_id": f"sim-{account[-3:]}-{kind_i+1:02d}", "account_id": account,
                    "region": REGIONS[(account_i + kind_i) % len(REGIONS)], "category": category,
                    "service": service_by_finding[finding], "team": team_by_finding[finding],
                    "environment": environment_by_finding[finding],
                    "finding": finding, "current_monthly_cost_usd": f"{cost:.2f}",
                    "estimated_savings_usd": f"{cost * rng.uniform(.45,.85):.2f}", "utilization_pct": str(utilization),
                    "evidence": evidence, "recommended_action": action,
                    "confidence": rng.choice(["High", "Medium", "Low"]), "status": "Open",
                    "dataset_note": DATASET_NOTE})
    return results


def generate_synthetic_budgets(seed: int = DEFAULT_SEED) -> list[dict[str, str]]:
    """Generate illustrative monthly team budgets, explicitly not AWS budgets."""
    rng = random.Random(seed + 900)
    budgets = {"Platform": 2200, "Data": 1350, "Commerce": 1250, "Security": 500}
    return [{"billing_month": f"2025-{month:02d}", "team": team,
             "budget_usd": f"{amount * rng.uniform(.96,1.04):.2f}",
             "dataset_note": DATASET_NOTE}
            for month in range(1,13) for team, amount in budgets.items()]


def generate_commitment_coverage(seed: int = DEFAULT_SEED) -> list[dict[str, str]]:
    """Generate synthetic Savings Plan/RI metrics for demonstration only."""
    rng = random.Random(seed + 1200)
    return [{"account_id": account, "commitment_type": kind,
             "coverage_pct": f"{rng.uniform(48, 91):.1f}",
             "utilization_pct": f"{rng.uniform(62, 98):.1f}",
             "dataset_note": DATASET_NOTE}
            for account in ACCOUNTS for kind in ("Savings Plan", "Reserved Instance")]


def write_synthetic_billing(path: str | Path = DEFAULT_OUTPUT, *, seed: int = DEFAULT_SEED) -> int:
    """Generate and write the explicitly synthetic dataset, returning its row count."""
    rows = generate_synthetic_billing(seed=seed)
    validate_synthetic_billing(rows)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    sidecars = {
        "ground_truth_anomalies.csv": generate_ground_truth(),
        "simulated_resource_findings.csv": generate_resource_findings(seed),
        "illustrative_team_budgets.csv": generate_synthetic_budgets(seed),
        "simulated_commitment_coverage.csv": generate_commitment_coverage(seed),
    }
    for filename, data in sidecars.items():
        if data:
            with (output.parent / filename).open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(data[0]))
                writer.writeheader()
                writer.writerows(data)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    count = write_synthetic_billing(args.output, seed=args.seed)
    print(f"Wrote {count:,} synthetic billing rows to {args.output} (seed {args.seed}).")
    print("No actual AWS billing data was read or written.")


if __name__ == "__main__":
    main()
