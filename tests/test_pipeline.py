import csv
from decimal import Decimal
from pathlib import Path

import pytest

from cloud_cost_analytics.aggregation import aggregate_by_service_month
from cloud_cost_analytics.anomalies import detect_spending_anomalies
from cloud_cost_analytics.pipeline import run_pipeline
from cloud_cost_analytics.validation import DataValidationError, load_billing_data


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT_ROOT / "data" / "raw" / "fictional_aws_billing.csv"


def test_sample_loads_and_is_explicitly_fictional():
    records = load_billing_data(SAMPLE)
    assert len(records) == 18
    assert all(record.is_fictional for record in records)


def test_aggregation_sums_by_month_and_service():
    records = load_billing_data(SAMPLE)
    rows = aggregate_by_service_month(records)
    assert len(rows) == 18
    assert next(row for row in rows if row["billing_month"] == "2025-01" and row["service"] == "Amazon EC2")["total_cost_usd"] == "120.00"


def test_anomaly_rule_flags_ec2_june_only():
    rows = aggregate_by_service_month(load_billing_data(SAMPLE))
    anomalies = detect_spending_anomalies(rows)
    assert [(row["billing_month"], row["service"]) for row in anomalies] == [("2025-06", "Amazon EC2")]
    assert anomalies[0]["baseline_cost_usd"] == "130.00"
    assert anomalies[0]["increase_usd"] == "290.00"


def test_nonfictional_data_is_rejected(tmp_path):
    bad_file = tmp_path / "not_fictional.csv"
    text = SAMPLE.read_text(encoding="utf-8").replace(",true,", ",false,", 1)
    bad_file.write_text(text, encoding="utf-8")
    with pytest.raises(DataValidationError, match="fictional data only"):
        load_billing_data(bad_file)


def test_negative_cost_is_rejected(tmp_path):
    bad_file = tmp_path / "negative.csv"
    text = SAMPLE.read_text(encoding="utf-8").replace(",120.00,true,", ",-1.00,true,", 1)
    bad_file.write_text(text, encoding="utf-8")
    with pytest.raises(DataValidationError, match="non-negative"):
        load_billing_data(bad_file)


def test_pipeline_writes_tableau_csvs(tmp_path):
    records, flagged = run_pipeline(SAMPLE, tmp_path)
    assert (records, flagged) == (18, 1)
    with (tmp_path / "service_monthly_costs.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 18
    assert Decimal(rows[0]["total_cost_usd"]) >= 0
    with (tmp_path / "spending_anomalies.csv").open(encoding="utf-8", newline="") as file:
        anomaly_rows = list(csv.DictReader(file))
    assert len(anomaly_rows) == 1
