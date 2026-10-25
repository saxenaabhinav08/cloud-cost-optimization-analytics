from datetime import datetime

import pandas as pd
import pytest

from cloud_cost_analytics.ask_ai import (
    UnsafeQuery, _QUERIES, match_intent, run_question, validate_readonly_sql,
)


@pytest.mark.parametrize("query", [
    "DROP TABLE billing_records",
    "UPDATE billing_records SET cost_usd=0",
    "SELECT service FROM billing_records; DROP TABLE billing_records",
    "SELECT * FROM secrets",
    "SELECT password FROM billing_records",
    "SELECT service FROM billing_records -- ignore policy",
    "SELECT service /* hidden text */ FROM billing_records",
    "PRAGMA table_info(billing_records)",
])
def test_generated_sql_rejects_write_injection_and_allowlist_bypasses(query):
    with pytest.raises(UnsafeQuery):
        validate_readonly_sql(query)


def test_readonly_validator_adds_and_caps_limit():
    assert validate_readonly_sql("SELECT service FROM billing_records").endswith("LIMIT 500")
    assert validate_readonly_sql("SELECT service FROM billing_records LIMIT 900") .endswith("LIMIT 500")


def test_offline_question_library_contains_twelve_intents_and_followup():
    assert len(_QUERIES) >= 12
    assert match_intent("break that down by team", "top_service") == "team_breakdown"


def _mini_frame():
    return pd.DataFrame([
        {"billing_date": datetime(2025, 1, 2), "billing_month": "2025-01", "service": "Amazon EC2", "cost_usd": 30.0,
         "region": "us-east-1", "environment": "prod", "team": "Platform", "project": "app", "account_id": "fictional-1",
         "usage_type": "Compute", "resource_id": "r-1", "resource_state": "active", "scenario": "", "dataset_note": "SYNTHETIC fictional"},
        {"billing_date": datetime(2025, 1, 3), "billing_month": "2025-01", "service": "Amazon S3", "cost_usd": 10.0,
         "region": "us-west-2", "environment": "", "team": "", "project": "bucket", "account_id": "fictional-1",
         "usage_type": "Storage", "resource_id": "r-2", "resource_state": "active", "scenario": "", "dataset_note": "SYNTHETIC fictional"},
        {"billing_date": datetime(2025, 2, 2), "billing_month": "2025-02", "service": "Amazon EC2", "cost_usd": 45.0,
         "region": "us-east-1", "environment": "prod", "team": "Platform", "project": "app", "account_id": "fictional-1",
         "usage_type": "Compute", "resource_id": "r-1", "resource_state": "active", "scenario": "", "dataset_note": "SYNTHETIC fictional"},
    ])


@pytest.mark.parametrize("question,intent", [
    ("Which service costs the most?", "top_service"),
    ("How did spend change last month?", "spend_change"),
    ("Why did spend move?", "movers"),
    ("Show untagged spend by team", "untagged"),
    ("What anomaly should I review?", "anomaly"),
    ("Show savings opportunities", "savings"),
    ("Which team has budget risk?", "budget"),
    ("Show spend by region", "region"),
    ("Show spend by environment", "environment"),
    ("Forecast month-end spend", "forecast"),
    ("List idle resources", "idle"),
    ("What is week-over-week spend?", "weekly"),
])
def test_each_canned_question_runs_read_only_sql(question, intent):
    result = run_question(question, _mini_frame())
    assert result["intent"] == intent
    assert result["sql"].lstrip().upper().startswith(("SELECT", "WITH"))
    assert result["rows"] == [] or isinstance(result["rows"], list)
    assert result["answer"].strip()


def test_unknown_question_does_not_invent_a_result():
    result = run_question("What color is the AWS logo?", _mini_frame())
    assert result["intent"] is None
    assert result["sql"] is None
    assert "couldn't match" in result["answer"]
