from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
from streamlit.testing.v1 import AppTest

from cloud_cost_analytics import dashboard_data
from cloud_cost_analytics.dashboard_data import (
    build_dashboard_data, load_sample_records, monthly_totals, region_totals,
    service_totals, summarize_spending,
)
from cloud_cost_analytics.streamlit_app import _anomaly_table, _breakdown_chart, _monthly_chart
from cloud_cost_analytics.finops_app import _service_movers


def test_dashboard_uses_shared_analysis_and_prepares_fictional_data():
    result = build_dashboard_data(load_sample_records())
    assert result.record_count == 18
    assert len(result.monthly_costs) == 18
    assert result.anomalies[0]["service"] == "Amazon EC2"
    assert result.anomalies[0]["billing_month"] == "2025-06"
    assert result.anomalies[0]["percentage_change"] == "223.08"


def test_summary_and_chart_helpers_aggregate_values():
    data = build_dashboard_data(load_sample_records())
    summary = summarize_spending(data.monthly_costs)
    assert summary.total_spend == Decimal("1729.00")
    assert summary.latest_month == "2025-06"
    assert summary.latest_month_spend == Decimal("538.00")
    assert summary.previous_month == "2025-05"
    assert summary.month_over_month_change == Decimal("290.00")
    assert f"{summary.month_over_month_percent:.2f}" == "116.94"
    june = {"2025-06"}
    assert monthly_totals(data.monthly_costs, june) == [{"billing_month": "2025-06", "total_cost_usd": "538.00"}]
    assert service_totals(data.monthly_costs, june)[0] == {"service": "Amazon EC2", "total_cost_usd": "420.00"}
    assert region_totals(data.region_costs, june) == [{"region": "us-east-1", "total_cost_usd": "538.00"}]


def test_empty_summary_is_safe():
    result = summarize_spending([])
    assert result.total_spend == Decimal("0.00")
    assert result.latest_month is None
    assert result.month_over_month_percent is None


def test_mysql_reader_maps_rows_and_disposes_engine(monkeypatch):
    engine = MagicMock()
    connection = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    connection.execute.return_value.mappings.return_value.all.return_value = [{
        "billing_month": date(2025, 1, 1), "account_id": "fictional-001",
        "region": "us-east-1", "service": "Amazon EC2", "usage_type": "Compute",
        "cost_usd": Decimal("12.50"), "is_fictional": True,
        "dataset_note": "Fictional development record",
    }]
    monkeypatch.setattr(dashboard_data, "create_db_engine", lambda: engine)
    rows = dashboard_data.load_mysql_records()
    assert rows[0].billing_month == "2025-01"
    assert rows[0].cost_usd == Decimal("12.50")
    assert rows[0].is_fictional is True
    sql = str(connection.execute.call_args.args[0])
    assert "WHERE is_fictional = TRUE" in sql
    engine.dispose.assert_called_once_with()


def test_streamlit_app_renders_default_fictional_mode():
    project_root = dashboard_data.PROJECT_ROOT
    app = AppTest.from_file(str(project_root / "app.py"), default_timeout=15).run()
    assert not app.exception
    assert any("Cloud cost overview" == item.value for item in app.title)
    assert [item.label for item in app.metric][:5] == [
        "Latest month spend", "Month-end forecast", "Budget used", "Estimated savings / month", "Open anomalies"
    ]
    assert len(app.metric[0].value) > 1
    assert any("Top insights" in item.value for item in app.subheader)


def test_streamlit_app_renders_local_mysql_mode(monkeypatch):
    from cloud_cost_analytics import finops_app

    monkeypatch.setattr(finops_app, "load_mysql_records", load_sample_records)
    app = AppTest.from_file(str(dashboard_data.PROJECT_ROOT / "app.py"), default_timeout=15)
    app.run()
    app.selectbox[0].set_value("Local MySQL").run()
    assert not app.exception
    assert any("MySQL schema stores monthly billing rows" in item.value for item in app.info)
    assert app.title[0].value == "Cloud cost overview"


def test_streamlit_app_reports_mysql_failure_without_database_details(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    from cloud_cost_analytics import finops_app

    def fail_to_connect():
        raise SQLAlchemyError("password=must-not-be-shown")

    monkeypatch.setattr(finops_app, "load_mysql_records", fail_to_connect)
    app = AppTest.from_file(str(dashboard_data.PROJECT_ROOT / "app.py"), default_timeout=15).run()
    app.selectbox[0].set_value("Local MySQL").run()
    assert any("Could not load selected data" in item.value for item in app.error)
    assert not any("must-not-be-shown" in item.value for item in app.error)


def test_sidebar_navigation_switches_single_page_sections():
    app = AppTest.from_file(str(dashboard_data.PROJECT_ROOT / "app.py"), default_timeout=15).run()
    nav = {item.label: item for item in app.button if item.key and item.key.startswith("finops-nav-")}
    assert set(nav) == {"Overview", "Cost Trends", "Service Breakdown", "Anomalies", "Optimization", "Budgets & Forecasts", "Ask AI", "Billing Records"}
    assert app.selectbox[0].value == "Synthetic demo"
    nav["Cost Trends"].click().run()
    assert app.title[0].value == "Cost Trends"
    assert any(item.label == "Time grain" for item in app.radio)
    next(item for item in app.button if item.key == "finops-nav-billing-records").click().run()
    assert app.title[0].value == "Billing Records"
    assert any("matching records" in item.value for item in app.caption)


def test_every_navigation_destination_renders_its_own_content():
    app = AppTest.from_file(str(dashboard_data.PROJECT_ROOT / "app.py"), default_timeout=20).run()
    expected = {
        "Cost Trends": "Time grain",
        "Service Breakdown": "Service",
        "Anomalies": "Review queue",
        "Optimization": "Optimization Opportunities",
        "Budgets & Forecasts": "Monthly budget and forecast by team",
        "Ask AI": "Suggested questions",
        "Billing Records": "Search records",
    }
    for page, content in expected.items():
        key = "finops-nav-" + page.lower().replace(" ", "-").replace("&", "and")
        next(item for item in app.button if item.key == key).click().run()
        rendered = [item.value for item in list(app.title) + list(app.subheader)]
        rendered += [item.label for item in list(app.radio) + list(app.selectbox) + list(app.text_input)]
        assert any(content in value for value in rendered), page


def test_billing_month_range_slider_preserves_date_filtering():
    app = AppTest.from_file(str(dashboard_data.PROJECT_ROOT / "app.py"), default_timeout=15).run()
    app.date_input[0].set_value((date(2025, 5, 1), date(2025, 6, 1))).run()
    assert not app.exception
    assert not app.exception


def test_dashboard_charts_include_clear_axes_and_currency_tooltips():
    monthly = pd.DataFrame([{"billing_month": "2025-01", "total_cost_usd": 125.0}])
    trend = _monthly_chart(monthly).to_dict()
    assert trend["encoding"]["x"]["title"] == "Billing month"
    assert trend["encoding"]["y"]["title"] == "Spend (USD)"
    assert any(item.get("format") == "$,.2f" for item in trend["encoding"]["tooltip"])

    service = pd.DataFrame([{"service": "Amazon EC2", "total_cost_usd": 125.0}])
    breakdown = _breakdown_chart(service, "service", "AWS service").to_dict()
    assert breakdown["encoding"]["y"]["title"] == "AWS service"
    assert breakdown["encoding"]["x"]["axis"]["format"] == "$,.0f"
    assert any(item.get("format") == "$,.2f" for item in breakdown["encoding"]["tooltip"])


def test_anomaly_table_contains_supported_fields_and_calculated_values():
    result = build_dashboard_data(load_sample_records())
    table = _anomaly_table(result.anomalies)
    assert list(table.columns) == ["Service", "Billing Month", "Current Cost", "Baseline", "Absolute Change", "Change (%)"]
    assert table.iloc[0]["Current Cost"] == 420.0
    assert table.iloc[0]["Baseline"] == 130.0
    assert table.iloc[0]["Absolute Change"] == 290.0
    assert table.iloc[0]["Change (%)"] == 223.08


def test_service_movers_compare_latest_two_months():
    frame = pd.DataFrame([
        {"billing_month":"2025-01", "service":"EC2", "cost_usd":10.0},
        {"billing_month":"2025-02", "service":"EC2", "cost_usd":15.0},
        {"billing_month":"2025-01", "service":"S3", "cost_usd":20.0},
        {"billing_month":"2025-02", "service":"S3", "cost_usd":12.0},
    ])
    movers = _service_movers(frame)
    assert movers.iloc[0].service == "EC2"
    assert movers.iloc[0].change_usd == 5.0
    assert movers.iloc[-1].change_usd == -8.0
