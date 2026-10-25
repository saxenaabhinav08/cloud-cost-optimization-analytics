from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from cloud_cost_analytics.charts import PALETTE, SERVICE_COLORS, format_usd, plotly_figure_to_echarts
from cloud_cost_analytics.dashboard_data import load_sample_records
from cloud_cost_analytics.finops_analytics import normalize_mysql_records, synthetic_frame
from cloud_cost_analytics.finops_app import (
    _composition_options, _overview_trend_options, _service_bar_options,
)


def test_shared_plotly_conversion_uses_accessible_echarts_palette_and_motion_setting():
    fig = go.Figure(go.Scatter(x=["2025-01", "2025-02"], y=[10, 12], name="Spend"))
    animated = plotly_figure_to_echarts(fig)
    still = plotly_figure_to_echarts(fig, reduce_motion=True)
    assert animated["animation"] is True
    assert still["animation"] is False
    assert animated["color"] == PALETTE
    assert len(animated["color"]) == 8
    assert animated["series"][0]["data"] == [10, 12]


def test_plotly_six_typed_arrays_expand_to_echarts_lists():
    fig = go.Figure(go.Bar(x=["a", "b", "c"], y=[12.5, 30.0, 48.25], name="Costs"))
    option = plotly_figure_to_echarts(fig)
    assert option["series"][0]["data"] == [12.5, 30.0, 48.25]
    assert option["xAxis"]["data"] == ["a", "b", "c"]


def test_shared_service_colors_and_usd_formatter_are_stable():
    assert SERVICE_COLORS["Amazon EC2"] == SERVICE_COLORS["Amazon EC2"]
    assert SERVICE_COLORS["Amazon S3"] != SERVICE_COLORS["Amazon EC2"]
    assert format_usd(11500) == "$11,500"
    assert format_usd(11500, decimals=2) == "$11,500.00"
    assert format_usd(11500, compact=True) == "$11.5K"


def test_iso_datetime_axis_labels_are_human_readable():
    fig = go.Figure(go.Scatter(
        x=["2025-01-06T00:00:00.000000000", "2025-02-03T00:00:00.000000000"],
        y=[100, 125], name="Amazon EC2",
    ))
    option = plotly_figure_to_echarts(fig)
    assert option["xAxis"]["data"] == ["Jan 06", "Feb 03"]


def test_horizontal_bar_conversion_keeps_numeric_values_on_value_axis():
    fig = go.Figure(go.Bar(
        x=[1250.0, 3400.0], y=["Storage", "Compute"], orientation="h", name="Savings",
    ))
    option = plotly_figure_to_echarts(fig)
    assert option["xAxis"]["type"] == "value"
    assert option["yAxis"]["type"] == "category"
    assert option["yAxis"]["data"] == ["Storage", "Compute"]
    assert option["series"][0]["data"] == [1250.0, 3400.0]
    assert option["xAxis"]["nameLocation"] == "middle"


def test_overview_weekly_option_uses_service_stacks_and_forecast_band():
    frame = synthetic_frame()
    options, title, subtitle = _overview_trend_options(frame, "Weekly", pd.DataFrame())
    assert "drive" in title
    assert "USD" in subtitle
    assert options["xAxis"]["data"]
    assert any(series.get("stack") == "actual-spend" for series in options["series"])
    assert any(series.get("name") == "Forecast (next 30 days)" for series in options["series"])


def test_service_bar_ranks_categories_and_composition_returns_donut_totals():
    frame = normalize_mysql_records(load_sample_records())
    bars, title, _ = _service_bar_options(frame)
    donut, rows, total = _composition_options(frame, "Service")
    assert title
    assert bars["series"][0]["type"] == "bar"
    assert donut["series"][0]["type"] == "pie"
    assert donut["series"][0]["radius"] == ["57%", "78%"]
    assert round(total, 2) == round(frame.cost_usd.sum(), 2)
    assert list(rows.columns) == ["Category", "Spend", "Share"]


def test_overview_service_bar_starts_at_zero_and_preserves_proportional_values():
    frame = normalize_mysql_records(load_sample_records())
    option, _, _ = _service_bar_options(frame)
    values = option["_spend_rows"]
    assert option["xAxis"]["min"] == 0
    assert option["xAxis"]["max"] >= max(row["value"] for row in values)
    assert max(row["value"] for row in values) / option["xAxis"]["max"] >= .9
    # Chart points are the actual USD totals, not percentages or normalized values.
    expected = frame.groupby("service").cost_usd.sum().sort_values(ascending=False).head(8)
    assert values[0]["value"] == expected.iloc[0]
    assert values[1]["value"] == expected.iloc[1]
    assert values[0]["value"] / values[1]["value"] == expected.iloc[0] / expected.iloc[1]
    assert values[-1]["name"] == "Other"
    assert values[-1]["itemStyle"]["color"] == "#B8C2CC"


def test_synthetic_service_mom_deltas_are_computed_and_not_identical():
    from cloud_cost_analytics.finops_app import _movers_by
    movers = _movers_by(synthetic_frame(), "service")
    deltas = movers.change_pct.dropna().round(2)
    assert len(deltas) >= 8
    assert deltas.nunique() >= 6
