"""Command-line pipeline that validates, analyzes, and exports Tableau CSVs."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .aggregation import aggregate_by_service_month
from .anomalies import detect_spending_anomalies
from .validation import load_billing_data


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    """Write rows with stable headers, including when there are no results."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def run_pipeline(input_path: Path, output_dir: Path) -> tuple[int, int]:
    """Run analysis and return (input record count, anomaly count)."""
    records = load_billing_data(input_path)
    monthly = aggregate_by_service_month(records)
    anomalies = detect_spending_anomalies(monthly)

    write_csv(
        output_dir / "service_monthly_costs.csv",
        monthly,
        ["billing_month", "service", "total_cost_usd"],
    )
    write_csv(
        output_dir / "spending_anomalies.csv",
        anomalies,
        [
            "billing_month", "service", "current_cost_usd", "baseline_cost_usd",
            "increase_usd", "rule", "review_note",
        ],
    )
    return len(records), len(anomalies)


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=project_root / "data" / "raw" / "fictional_aws_billing.csv",
        help="Path to the explicitly fictional development billing CSV.",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=project_root / "data" / "processed",
        help="Directory for Tableau-ready CSV outputs.",
    )
    args = parser.parse_args()
    try:
        record_count, anomaly_count = run_pipeline(args.input, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Processed {record_count} fictional billing records.")
    print(f"Wrote service_monthly_costs.csv and spending_anomalies.csv to {args.output_dir}")
    print(f"Flagged {anomaly_count} fictional-data anomaly/anomalies for review.")


if __name__ == "__main__":
    main()
