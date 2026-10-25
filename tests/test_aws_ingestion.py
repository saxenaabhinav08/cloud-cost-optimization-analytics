from __future__ import annotations

from datetime import date
from decimal import Decimal
import logging
import os
import socket

import pytest
from sqlalchemy import create_engine

from cloud_cost_analytics.ingestion.aws.athena_cur import AthenaCurClient, plan_athena_query
from cloud_cost_analytics.ingestion.aws.config import AwsConfig, load_aws_config
from cloud_cost_analytics.ingestion.aws.cost_explorer import CostExplorerClient, build_group_by
from cloud_cost_analytics.ingestion.aws.errors import (
    AwsAccessDeniedError, AwsIngestionDisabledError, AwsRequestError, ConfigError,
    MalformedRatioExceededError, MixedCurrencyError, RequestBudgetExceededError,
    ScanLimitExceededError,
)
from cloud_cost_analytics.ingestion.aws.normalize import normalize_records, to_analytics_frame
from cloud_cost_analytics.ingestion.aws.pipeline import AwsIngestionPipeline, authoritative_records, select_resync_scopes
from cloud_cost_analytics.ingestion.aws.redact import RedactingFilter, redact_text
from cloud_cost_analytics.ingestion.aws.repository import AwsRepository, create_aws_schema


def enabled(**kwargs):
    defaults = dict(enabled=True, max_api_requests=20, max_pages=10, max_retry_attempts=2,
                    athena_database="cur_db", athena_table="cur_table", athena_workgroup="wg",
                    athena_output_location="s3://billing-results/query/")
    defaults.update(kwargs)
    return AwsConfig(**defaults)


def source_row(**kwargs):
    row = {"aws_account_id": "123456789012", "usage_date": "2026-01-03", "service": "Amazon EC2",
           "region": "us-east-1", "usage_type": "BoxUsage", "cost": "12.34567891", "currency": "USD",
           "tags": {"team": "platform"}}
    row.update(kwargs)
    return row


def test_autouse_fixture_blocks_sockets_and_removes_aws_environment(monkeypatch):
    assert "AWS_ACCESS_KEY_ID" not in __import__("os").environ
    with pytest.raises(AssertionError, match="external network"):
        socket.socket().connect(("example.com", 443))


def test_default_configuration_is_disabled_and_never_needs_boto3(monkeypatch):
    monkeypatch.delenv("AWS_INGESTION_ENABLED", raising=False)
    assert "AWS_INGESTION_ENABLED" not in os.environ
    config = load_aws_config(environ=os.environ, secrets={})
    assert config.enabled is False
    with pytest.raises(AwsIngestionDisabledError):
        CostExplorerClient(config)


def test_config_validation_reports_setting_names_not_values():
    with pytest.raises(ConfigError) as error:
        load_aws_config({"AWS_INGESTION_ENABLED": "true", "AWS_ATHENA_DATABASE": "bad;value"}, {})
    assert "AWS_ATHENA_DATABASE" in str(error.value)
    assert "bad;value" not in str(error.value)


def test_grouping_limit_and_account_are_enforced():
    groups = build_group_by(["SERVICE"])
    assert [group["Key"] for group in groups] == ["LINKED_ACCOUNT", "SERVICE"]
    with pytest.raises(ValueError, match="at most two"):
        build_group_by(["SERVICE", "REGION"])


class FakeCE:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class AwsFakeError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def test_cost_explorer_pagination_decimal_and_estimated_flag():
    fake = FakeCE([
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
          "Estimated": True, "Groups": [{"Keys": ["123456789012", "Amazon EC2"],
          "Metrics": {"UnblendedCost": {"Amount": "1.2300", "Unit": "USD"}}}]}], "NextPageToken": "next"},
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
          "Groups": [{"Keys": ["123456789012", "Amazon S3"],
          "Metrics": {"UnblendedCost": {"Amount": "2.00", "Unit": "USD"}}}]}]},
    ])
    client = CostExplorerClient(enabled(), fake, clock_fn=lambda: date(2026, 1, 10), sleep_fn=lambda _: None)
    rows = client.fetch(date(2026, 1, 1), date(2026, 1, 2))
    assert len(rows) == 2
    assert rows[0]["cost"] == Decimal("1.2300") and rows[0]["is_estimated"] is True
    assert len(fake.calls) == 2


def test_cost_explorer_rejects_future_range_and_honors_request_budget():
    fake = FakeCE([{}])
    client = CostExplorerClient(enabled(), fake, clock_fn=lambda: date(2026, 1, 10))
    with pytest.raises(ValueError, match="future"):
        client.fetch(date(2026, 1, 1), date(2026, 1, 11))
    client.config = enabled(max_api_requests=0)
    # Config's public bounds are validated by the loader; the client still has a hard stop.
    with pytest.raises(RequestBudgetExceededError):
        client.fetch(date(2026, 1, 1), date(2026, 1, 2))


@pytest.mark.parametrize("failure", [AwsFakeError("ThrottlingException"), AwsFakeError("InternalError", 503)])
def test_cost_explorer_retries_throttle_and_server_error_only(failure):
    fake = FakeCE([failure, {}])
    client = CostExplorerClient(enabled(), fake, clock_fn=lambda: date(2026, 1, 10),
                                sleep_fn=lambda _: None, jitter_fn=lambda *_: 0)
    client.fetch(date(2026, 1, 1), date(2026, 1, 2))
    assert len(fake.calls) == 2


@pytest.mark.parametrize("failure,expected", [
    (AwsFakeError("AccessDeniedException"), AwsAccessDeniedError),
    (ConnectionError("offline"), AwsRequestError),
])
def test_cost_explorer_does_not_retry_access_or_network_errors(failure, expected):
    fake = FakeCE([failure, {}])
    client = CostExplorerClient(enabled(), fake, clock_fn=lambda: date(2026, 1, 10),
                                sleep_fn=lambda _: None, jitter_fn=lambda *_: 0)
    with pytest.raises(expected):
        client.fetch(date(2026, 1, 1), date(2026, 1, 2))
    assert len(fake.calls) == 1


def test_cost_explorer_stops_at_page_cap_before_another_request():
    page = {"ResultsByTime": [], "NextPageToken": "next"}
    fake = FakeCE([page, page])
    client = CostExplorerClient(enabled(max_pages=1), fake, clock_fn=lambda: date(2026, 1, 10))
    with pytest.raises(AwsRequestError, match="page cap"):
        client.fetch(date(2026, 1, 1), date(2026, 1, 2))
    assert len(fake.calls) == 1


@pytest.mark.parametrize("fmt", ["legacy_cur", "cur2"])
def test_athena_plan_uses_partition_predicates_and_bound_values(fmt):
    plan = plan_athena_query(database="cur_db", table="cur_table", cur_format=fmt,
        cost_metric="UnblendedCost", year=2026, month=1,
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), tag_key="team")
    assert '"year" = ?' in plan.sql and '"month" = ?' in plan.sql
    assert "team" not in plan.sql
    assert plan.parameters[-1] == "team"
    assert "'team'" in plan.execution_parameters()[-1]


def test_athena_plan_rejects_identifier_and_tag_injection():
    with pytest.raises(ConfigError):
        plan_athena_query(database="cur;DROP", table="x", cur_format="cur2", cost_metric="UnblendedCost",
            year=2026, month=1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
    with pytest.raises(ValueError, match="tag key"):
        plan_athena_query(database="db", table="tb", cur_format="cur2", cost_metric="UnblendedCost",
            year=2026, month=1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), tag_key="x' OR 1=1")


class FakeAthena:
    def __init__(self):
        self.polls = 0
        self.results = 0

    def start_query_execution(self, **kwargs):
        self.start = kwargs
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **kwargs):
        self.polls += 1
        state = "RUNNING" if self.polls == 1 else "SUCCEEDED"
        return {"QueryExecution": {"Status": {"State": state},
                "Statistics": {"DataScannedInBytes": 100}}}

    def get_query_results(self, **kwargs):
        self.results += 1
        if self.results == 1:
            return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": "cost"}]},
                    "Rows": [{"Data": [{"VarCharValue": "cost"}]}, {"Data": [{"VarCharValue": "3.50"}]}]},
                    "NextToken": "next"}
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "4.50"}]}]}}


def test_athena_execution_polls_paginates_and_respects_dry_run():
    fake = FakeAthena()
    client = AthenaCurClient(enabled(), fake, sleep_fn=lambda _: None, clock_fn=lambda: 0)
    plan = client.plan(year=2026, month=1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31))
    assert client.query(plan, dry_run=True) is plan
    assert not hasattr(fake, "start")
    assert client.query(plan) == [{"cost": "3.50"}, {"cost": "4.50"}]
    assert fake.polls == 2


def test_athena_scan_cap_stops_results_read():
    fake = FakeAthena()
    fake.get_query_execution = lambda **kwargs: {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
        "Statistics": {"DataScannedInBytes": 101}}}
    client = AthenaCurClient(enabled(max_bytes_scanned=100), fake, clock_fn=lambda: 0)
    plan = client.plan(year=2026, month=1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
    with pytest.raises(ScanLimitExceededError):
        client.query(plan)
    assert fake.results == 0


def test_athena_enforces_shared_api_request_budget():
    fake = FakeAthena()
    client = AthenaCurClient(enabled(max_api_requests=1), fake, clock_fn=lambda: 0)
    plan = client.plan(year=2026, month=1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
    with pytest.raises(RequestBudgetExceededError):
        client.query(plan)
    assert hasattr(fake, "start") and fake.polls == 0


def test_athena_stops_at_results_page_cap_before_another_request():
    fake = FakeAthena()
    client = AthenaCurClient(enabled(max_pages=1), fake, sleep_fn=lambda _: None, clock_fn=lambda: 0)
    plan = client.plan(year=2026, month=1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
    with pytest.raises(AwsRequestError, match="page cap"):
        client.query(plan)
    assert fake.results == 1


def test_normalization_decimal_credits_tags_and_aggregation():
    report = normalize_records([
        source_row(cost="0.10"), source_row(cost="0.20"),
        source_row(cost="-0.05", usage_type="Credit", line_item_type="Credit")
    ], source="cur", tag_field_map={"team": "team"})
    assert len(report.records) == 2
    total = sum((record.cost for record in report.records), Decimal("0"))
    assert total == Decimal("0.25")
    frame = to_analytics_frame(report.records, tag_field_map={"team": "team"})
    assert set(frame.columns) == {"record_id", "billing_date", "billing_month", "account_id", "region", "service",
        "usage_type", "resource_id", "resource_state", "cost_usd", "team", "project", "environment", "scenario", "dataset_note"}
    assert "platform" in frame.team.values


def test_normalization_rejects_mixed_currency_batches():
    with pytest.raises(MixedCurrencyError, match="Mixed currencies"):
        normalize_records([source_row(currency="USD"), source_row(currency="EUR")], source="cur")


def test_normalization_quarantines_bad_rows_and_threshold_includes_samples():
    report = normalize_records([source_row(), source_row(cost="NaN")], source="cur", max_malformed_ratio=0.5)
    assert report.counts == {"invalid_cost": 1}
    assert report.sample_reasons == ("row 2: invalid_cost",)
    with pytest.raises(MalformedRatioExceededError, match="row 2: invalid_cost"):
        normalize_records([source_row(), source_row(cost="NaN")], source="cur", max_malformed_ratio=0.1)


def test_redaction_and_logging_filter_never_emit_credential_shaped_values(caplog):
    key = "A" + "KIA" + "A" * 16
    secret = "not-a-real-secret"
    raw = f"access_key_id={key} aws_" + "secret" + f"_access_key={secret} arn:aws:iam::123456789012:role/name"
    safe = redact_text(raw)
    assert key not in safe and secret not in safe and "123456789012" not in safe
    logger = logging.getLogger("test.redaction")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.ERROR, logger="test.redaction"):
        logger.error(raw)
    assert key not in caplog.text and secret not in caplog.text


def test_repository_idempotent_restatement_and_revision_history():
    engine = create_engine("sqlite://")
    create_aws_schema(engine)
    repo = AwsRepository(engine)
    record1 = normalize_records([source_row(cost="10.00")], source="cur").records[0]
    record2 = normalize_records([source_row(cost="12.00")], source="cur").records[0]
    first = repo.replace_scope(source="cur", scope="2026-01", records=[record1], run_id="run-one")
    again = repo.replace_scope(source="cur", scope="2026-01", records=[record1], run_id="run-two")
    revised = repo.replace_scope(source="cur", scope="2026-01", records=[record2], run_id="run-three")
    assert first.inserted == 1 and again.unchanged == 1 and again.inserted == 0
    assert revised.updated == 1
    history = repo.revisions_for_key(record1.record_key)
    assert len(history) == 1 and Decimal(history[0]["previous_cost"]) == Decimal("10.00")


def test_repository_restate_removes_disappeared_records_from_scope():
    engine = create_engine("sqlite://")
    create_aws_schema(engine)
    repo = AwsRepository(engine)
    old = normalize_records([source_row(cost="5.00")], source="cur").records[0]
    repo.replace_scope(source="cur", scope="2026-01", records=[old], run_id="run-one")
    result = repo.replace_scope(source="cur", scope="2026-01", records=[], run_id="run-two")
    assert result.deleted == 1
    assert repo.rows_for_scope(source="cur", scope="2026-01") == []


def test_repository_scope_failure_rolls_back_rows_and_revisions():
    engine = create_engine("sqlite://")
    create_aws_schema(engine)
    repo = AwsRepository(engine)
    rows = normalize_records([source_row(cost="4"), source_row(cost="6", service="Amazon S3")], source="cur").records
    with pytest.raises(RuntimeError, match="transactional failure"):
        repo.replace_scope("cur", "2026-01", rows, run_id="failed-run", fail_after=1)
    assert repo.rows_for_scope("cur", "2026-01") == []
    assert repo.revisions_for_key(rows[0].record_key) == []


def test_pipeline_success_is_idempotent_and_advances_watermark():
    engine = create_engine("sqlite://")
    create_aws_schema(engine)
    repo = AwsRepository(engine)

    class FakeReader:
        request_count = 1
        def fetch(self, start, end, **kwargs):
            return [source_row(usage_date=start.isoformat(), cost="7.25", source_version="fixture-v1")]

    pipeline = AwsIngestionPipeline(enabled(), repo, cost_explorer=FakeReader())
    first = pipeline.run(source="cost_explorer", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), run_id="pipeline-one")
    second = pipeline.run(source="cost_explorer", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), run_id="pipeline-two")
    assert first.rows_inserted == 1 and second.rows_unchanged == 1
    assert repo.watermark("cost_explorer", "2026-01")["last_successful_run_id"] == "pipeline-two"
    assert repo.run_row("pipeline-one")["status"] == "succeeded"


def test_pipeline_failure_does_not_advance_watermark_and_records_failed_run():
    engine = create_engine("sqlite://")
    create_aws_schema(engine)
    repo = AwsRepository(engine)

    class BadReader:
        request_count = 1
        def fetch(self, start, end, **kwargs):
            return [source_row(usage_date=start.isoformat(), cost="NaN")]

    pipeline = AwsIngestionPipeline(enabled(max_malformed_ratio=0), repo, cost_explorer=BadReader())
    with pytest.raises(MalformedRatioExceededError):
        pipeline.run(source="cost_explorer", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), run_id="pipeline-failed")
    assert repo.watermark("cost_explorer", "2026-01") is None
    assert repo.run_row("pipeline-failed")["status"] == "failed"


def test_resync_selection_and_source_authority_avoid_double_counting():
    scopes = select_resync_scopes(date(2026, 3, 10), resync_window_days=90,
        watermarks=[{"scope": "2025-12", "source_version": "old"}],
        observed_versions={"2025-12": "new"})
    assert scopes == ("2025-12", "2026-01", "2026-02", "2026-03")
    cur_record = normalize_records([source_row(source="cur")], source="cur").records[0]
    ce_record = normalize_records([source_row(source="cost_explorer")], source="cost_explorer").records[0]
    assert authoritative_records([cur_record, ce_record], preference="cur", cur_available_periods=["2026-01"]) == [cur_record]
    assert authoritative_records([cur_record, ce_record], preference="cur") == [ce_record]
