"""Distinct FinOps dashboard pages backed by synthetic daily analysis."""
from __future__ import annotations

from datetime import date
from html import escape
import json
import math
import os
from pathlib import Path
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError

from .dashboard_data import load_mysql_records
from .charts import PALETTE, color_for, format_usd, plotly_figure_to_echarts, render_chart, render_echarts_card
from .database import PROJECT_ROOT
from .styles import PRIMARY, finops_template, inject_styles
from .finops_analytics import (allocation_summary, anomaly_events, apply_filters,
    build_insights, budget_summary, commitment_coverage_frame, detection_quality,
    forecast_month_end, load_cur_csv, load_ground_truth, monthly_spend,
    monthly_savings_estimates, next_30_day_forecast, normalize_mysql_records,
    simulated_optimization_findings, synthetic_frame)
from .synthetic_data import DEFAULT_OUTPUT
from .ask_ai import generate_sql_with_openai, run_question

PAGES = ["Overview", "Cost Trends", "Service Breakdown", "Anomalies", "Optimization", "Budgets & Forecasts", "Ask AI", "Billing Records"]
PAGE_ICONS = {
    "Overview": ":material/dashboard:", "Cost Trends": ":material/show_chart:",
    "Service Breakdown": ":material/view_list:", "Anomalies": ":material/warning:",
    "Optimization": ":material/savings:", "Budgets & Forecasts": ":material/account_balance_wallet:",
    "Ask AI": ":material/chat:", "Billing Records": ":material/receipt_long:",
}
ACCENT = PRIMARY


def _chart_theme(fig):
    return finops_template(fig)


def _money(value) -> str:
    return format_usd(float(value))


def _service_movers(frame: pd.DataFrame) -> pd.DataFrame:
    """Compare the two latest represented months by service."""
    cols = ["service", "previous_usd", "current_usd", "change_usd", "change_pct"]
    if frame.empty or frame.billing_month.nunique() < 2:
        return pd.DataFrame(columns=cols)
    periods = sorted(frame.billing_month.unique())[-2:]
    wide = frame[frame.billing_month.isin(periods)].pivot_table(index="service", columns="billing_month", values="cost_usd", aggfunc="sum", fill_value=0)
    previous, current = periods
    wide = wide.reindex(columns=periods, fill_value=0)
    out = pd.DataFrame({"service": wide.index, "previous_usd": wide[previous].values, "current_usd": wide[current].values})
    out["change_usd"] = out.current_usd - out.previous_usd
    out["change_pct"] = out.apply(lambda row: row.change_usd / row.previous_usd * 100 if row.previous_usd else None, axis=1)
    return out[cols].sort_values("change_usd", ascending=False).reset_index(drop=True)


def _movers_by(frame: pd.DataFrame, dimension: str) -> pd.DataFrame:
    """Compute latest-month cost movement for a permitted billing dimension."""
    columns = [dimension, "previous_usd", "current_usd", "change_usd", "change_pct"]
    if frame.empty or frame.billing_month.nunique() < 2:
        return pd.DataFrame(columns=columns)
    periods = sorted(frame.billing_month.unique())[-2:]
    pivot = frame[frame.billing_month.isin(periods)].pivot_table(index=dimension, columns="billing_month", values="cost_usd", aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(columns=periods, fill_value=0)
    out = pd.DataFrame({dimension:pivot.index, "previous_usd":pivot[periods[0]].values, "current_usd":pivot[periods[1]].values})
    out["change_usd"] = out.current_usd - out.previous_usd
    out["change_pct"] = out.apply(lambda row: row.change_usd / row.previous_usd * 100 if row.previous_usd else None, axis=1)
    return out[columns].sort_values("change_usd", ascending=False).reset_index(drop=True)


@st.cache_data(show_spinner="Loading synthetic demo data…")
def _cached_synthetic_frame(file_modified_ns: int) -> pd.DataFrame:
    """Cache the generated source and invalidate when its file changes."""
    return synthetic_frame()


@st.cache_data(show_spinner=False)
def _cached_cur_frame(content: bytes) -> pd.DataFrame:
    return load_cur_csv(content)


def _sidebar() -> tuple[str, dict[str, object], str, pd.DataFrame]:
    inject_styles()
    with st.sidebar:
        st.markdown(":material/cloud: **Cloud Cost Analytics**")
        st.caption("FINOPS WORKSPACE")
        st.markdown("**Navigation**")
        if "finops_page" not in st.session_state:
            st.session_state.finops_page = "Overview"
        for page in PAGES:
            key = "finops-nav-" + page.lower().replace(" ", "-").replace("&", "and")
            st.button(page, key=key, icon=PAGE_ICONS[page], type="primary" if st.session_state.finops_page == page else "tertiary",
                width="stretch", on_click=lambda selected=page: setattr(st.session_state, "finops_page", selected))
        st.divider()
        st.markdown("**Data controls**")
        st.checkbox("Reduce motion", key="reduce_motion", value=False, help="Turn off chart and page entrance animations.")
        mode = st.selectbox("Data source", ["Synthetic demo", "Upload CUR CSV", "Local MySQL"], key="finops-mode")
        if mode == "Upload CUR CSV":
            uploaded_file = st.file_uploader("Upload CUR CSV", type=["csv"], help="Required: billing_date, account_id, region, service, usage_type, cost_usd. Common AWS CUR headers are supported.")
        else:
            uploaded_file = None
        st.button("AWS Cost Explorer (not connected)", disabled=True, help="AWS Cost Explorer is not connected in this project.", width="stretch")
    status = ""
    try:
        if mode == "Synthetic demo":
            modified = DEFAULT_OUTPUT.stat().st_mtime_ns if DEFAULT_OUTPUT.exists() else 0
            frame = _cached_synthetic_frame(modified)
            status = f"Synthetic demo data · {len(frame):,} daily records"
        elif mode == "Upload CUR CSV":
            frame = _cached_cur_frame(uploaded_file.getvalue()) if uploaded_file is not None else pd.DataFrame()
            status = f"Uploaded CUR · {len(frame):,} records" if not frame.empty else "Upload a CUR-format CSV to begin"
        else:
            frame = normalize_mysql_records(load_mysql_records())
            status = f"Local MySQL · connected · {len(frame):,} fictional records"
    except (SQLAlchemyError, OSError, ValueError) as exc:
        if mode == "Upload CUR CSV" and isinstance(exc, ValueError):
            st.error(str(exc))
        else:
            st.error(f"Could not load selected data ({type(exc).__name__}). Configuration details are hidden.")
        frame = pd.DataFrame()
        status = "Local MySQL · connection failed"
    with st.sidebar:
        if mode == "Upload CUR CSV":
            st.caption("Expected headers include billing date, account, region, service, usage type, and cost. Tags are optional.")
        elif mode == "Local MySQL":
            st.caption("Reads monthly records from the configured local database.")
        st.markdown("**Billing period**")
        if not frame.empty:
            lo, hi = frame.billing_date.min().date(), frame.billing_date.max().date()
            period = st.date_input("Date range", value=(lo, hi), min_value=lo, max_value=hi, key="finops-dates")
            start, end = period if isinstance(period, tuple) and len(period) == 2 else (lo, hi)
            filters = {"start": start, "end": end}
            for field, label in [("service", "Service"), ("account_id", "Account"), ("region", "Region"), ("team", "Team"), ("environment", "Environment")]:
                options = sorted(v for v in frame[field].dropna().unique() if str(v))
                if field in ("team", "environment") and (frame[field] == "").any():
                    options.append("Untagged")
                chosen = st.multiselect(label, options, key=f"finops-filter-{field}")
                if chosen:
                    if "Untagged" in chosen:
                        frame[field] = frame[field].replace("", "Untagged")
                    filters[field] = chosen
        else:
            filters = {}
        st.divider()
        if mode == "Synthetic demo":
            st.markdown(f'<div class="finops-card"><p class="finops-impact">{escape(status)}</p><p>Seeded fictional data · no AWS connection</p></div>', unsafe_allow_html=True)
        else:
            st.caption(f"Data status · {status}")
    return st.session_state.finops_page, filters, mode, frame


def _render_overview(frame: pd.DataFrame, mode: str):
    st.title("Cloud cost overview")
    if frame.empty:
        st.info("No records are available for this selection. Choose a data source or adjust the filters.")
        return
    frame = frame.sort_values("billing_date")
    months = monthly_spend(frame)
    latest = months.iloc[-1] if not months.empty else None
    previous = months.iloc[-2] if len(months) > 1 else None
    forecast = forecast_month_end(frame)
    anomalies = anomaly_events(frame)
    review_status = _load_review_status()
    if not anomalies.empty:
        anomalies = anomalies.copy()
        anomalies["status"] = [review_status.get(_anomaly_status_key(row), "Open") for row in anomalies.itertuples()]
    latest_period = frame[frame.billing_month == forecast["month"]]
    budgets = budget_summary(frame)
    budget_used = (budgets.actual_usd.sum() / budgets.budget_usd.sum() * 100) if not budgets.empty and budgets.budget_usd.sum() else None
    findings = simulated_optimization_findings(frame)
    savings = float(findings.estimated_savings_usd.sum()) if not findings.empty else 0.0
    savings_history = monthly_savings_estimates(frame)
    if len(months):
        counts = (anomalies[anomalies.status == "Open"].assign(month=anomalies.billing_date.dt.to_period("M").astype(str)).groupby("month").size()
                  if not anomalies.empty else pd.Series(dtype=int))
        anomaly_delta = int(counts.get(str(latest.billing_month), 0) - counts.get(str(previous.billing_month), 0)) if previous is not None else None
    else:
        anomaly_delta = None
    spend_delta = ((latest.cost_usd / previous.cost_usd - 1) * 100) if previous is not None and previous.cost_usd else None
    prior_frame = frame[frame.billing_month == (str(previous.billing_month) if previous is not None else "")]
    prior_budget = budget_summary(prior_frame)
    prior_util = (prior_budget.actual_usd.sum() / prior_budget.budget_usd.sum() * 100) if not prior_budget.empty and prior_budget.budget_usd.sum() else None
    budget_delta = budget_used - prior_util if budget_used is not None and prior_util is not None else None
    tiles = st.columns(5, gap="small")
    tiles[0].metric("Latest month spend", _money(latest.cost_usd) if latest is not None else "—",
                    delta=f"{spend_delta:+.1f}% vs prior month" if spend_delta is not None else None,
                    delta_color="inverse")
    forecast_delta = ((float(forecast["forecast"]) / float(previous.cost_usd) - 1) * 100
                      if previous is not None and previous.cost_usd else None)
    tiles[1].metric("Month-end forecast", _money(forecast["forecast"]),
                    delta=f"{forecast_delta:+.1f}% vs last month" if forecast_delta is not None else "No prior month", delta_color="inverse",
                    help="Projection = month-to-date actual spend ÷ elapsed days × calendar days in month.")
    tiles[2].metric("Budget used", f"{budget_used:.1f}%" if budget_used is not None else "N/A",
                    delta=f"{budget_delta:+.1f} pts vs prior month" if budget_delta is not None else "No comparable budget period",
                    delta_color="inverse")
    savings_delta = (float(savings_history.iloc[-1].estimated_savings_usd - savings_history.iloc[-2].estimated_savings_usd)
                     if len(savings_history) > 1 else None)
    tiles[3].metric("Estimated savings / month", _money(savings),
                    delta=f"{'+' if savings_delta >= 0 else '-'}{format_usd(abs(savings_delta))} vs prior month" if savings_delta is not None else "Simulated estimate",
                    delta_color="normal", help="Illustrative optimization estimate from fictional resource metadata; not realized savings.")
    open_count = int((anomalies.status == "Open").sum()) if not anomalies.empty else 0
    tiles[4].metric("Open anomalies", f"{open_count:,}",
                    delta=f"{anomaly_delta:+} vs prior month" if anomaly_delta is not None else None,
                    delta_color="inverse")

    grain = st.radio("Trend granularity", ["Daily", "Weekly", "Monthly"], index=1,
                     horizontal=True, key="overview-grain", label_visibility="collapsed")
    trend_options, takeaway, trend_subtitle = _overview_trend_options(frame, grain, anomalies)
    render_echarts_card(trend_options, takeaway, subtitle=trend_subtitle,
                        help_text="Stacked areas show service contributions. The dashed continuation is a modeled 30-day forecast; its shaded band is approximate, not a guarantee.",
                        key="overview-trend", height=430)

    bar_options, bar_title, bar_subtitle = _service_bar_options(frame)
    from streamlit_echarts import JsCode, st_echarts
    with st.container(border=True):
        st.markdown(f"**{bar_title}**")
        st.caption(bar_subtitle)
        chart_options = {key: value for key, value in bar_options.items() if not key.startswith("_")}
        # Its client parser expects a single-line JavaScript function placeholder.
        tooltip_callback = " ".join(bar_options["_tooltip_js"].splitlines())
        # streamlit-echarts detects this package's explicit placeholder string;
        # str(JsCode(...)) is only the object's Python repr and disables the JS.
        chart_options["tooltip"]["formatter"] = JsCode(tooltip_callback).js_code
        chart_options.update({"backgroundColor": "#FFFFFF", "animation": not bool(st.session_state.get("reduce_motion", False)),
                              "animationDuration": 800, "animationEasing": "cubicOut"})
        selected_service = st_echarts(chart_options, height=f"{max(300, 52 + 36 * bar_options['_row_count'])}px",
                                      events={"click": "function(params) { return params.name; }"},
                                      key=f"overview-service-bars-{st.session_state.get('overview-chart-reset', 0)}")
    if isinstance(selected_service, dict):
        selected_service = selected_service.get("name")
    top_services = frame.groupby("service").cost_usd.sum().sort_values(ascending=False).head(8).index
    other_frame = frame[~frame.service.isin(top_services)]
    if selected_service in frame.service.unique() or (selected_service == "Other" and not other_frame.empty):
        st.session_state["overview-selected-service"] = selected_service
    selected = st.session_state.get("overview-selected-service")
    if selected == "Other":
        analysis_frame = other_frame
    else:
        analysis_frame = frame[frame.service == selected] if selected in frame.service.unique() else frame
    if selected in frame.service.unique() or (selected == "Other" and not other_frame.empty):
        if st.button(f"Filtered to {selected} · Clear filter", key="overview-clear-service"):
            st.session_state.pop("overview-selected-service", None)
            st.session_state["overview-chart-reset"] = st.session_state.get("overview-chart-reset", 0) + 1
            st.rerun()

    with st.container(border=True):
        dimension = st.segmented_control("Composition", ["Service", "Team", "Account", "Region"],
                                         default="Service", key="overview-treemap-dimension")
        donut_options, donut_rows, donut_total = _composition_options(analysis_frame, dimension or "Service")
        donut_col, legend_col = st.columns([1.1, 1], vertical_alignment="center")
        with donut_col:
            render_echarts_card(donut_options, "Spend mix",
                                subtitle=f"{format_usd(donut_total, compact=True)} total spend",
                                help_text="Slices are sorted by spend. Categories below 3% are combined into Other. Hover to inspect the share.",
                                key="overview-composition", height=320)
        with legend_col:
            _render_donut_legend(donut_rows)

    st.subheader("Top insights")
    filtered_anomalies = anomaly_events(analysis_frame)
    filtered_findings = simulated_optimization_findings(analysis_frame)
    filtered_budgets = budget_summary(analysis_frame)
    insights = build_insights(analysis_frame, filtered_anomalies, filtered_findings, filtered_budgets)
    if not insights:
        st.info("There are not enough comparable records to calculate insight cards.")
    else:
        for offset in range(0, len(insights), 2):
            columns = st.columns(2, gap="medium")
            for column, insight in zip(columns, insights[offset:offset+2]):
                with column:
                    st.markdown(f'<div class="finops-card"><h4>{escape(insight["headline"])}</h4><p class="finops-impact">Impact: {escape(insight["impact"])}</p><p>Driver: {escape(insight["driver"])}</p><p><b>Recommended action:</b> {escape(insight["action"])}</p></div>', unsafe_allow_html=True)

    st.subheader("Recent anomaly flags")
    overview_flags = anomalies.head(5)[[
        "billing_date", "service", "observed_usd", "baseline_usd",
        "change_usd", "change_pct", "severity", "probable_driver",
    ]]
    _anomaly_table(overview_flags)


def _period_labels(dates: pd.Series, grain: str) -> pd.Series:
    values = pd.to_datetime(dates)
    if grain == "Daily":
        return values.dt.strftime("%Y-%m-%d")
    if grain == "Weekly":
        return (values.dt.to_period("W").dt.start_time).dt.strftime("%Y-%m-%d")
    return values.dt.to_period("M").astype(str)


def _overview_trend_options(frame: pd.DataFrame, grain: str, anomalies: pd.DataFrame):
    """Build a service-stacked overview with a forecast continuation and anomaly marks."""
    source = frame.copy()
    source["period"] = _period_labels(source.billing_date, grain)
    service_totals = source.groupby("service").cost_usd.sum().sort_values(ascending=False)
    top_services = service_totals.head(5).index.tolist()
    source["series_service"] = source.service.where(source.service.isin(top_services), "Other")
    pivot = source.groupby(["period", "series_service"]).cost_usd.sum().unstack(fill_value=0)
    historic_labels = pivot.index.astype(str).tolist()
    totals = pivot.sum(axis=1)
    top_two = service_totals.head(2)
    drivers = " and ".join(top_two.index.astype(str).tolist()) if not top_two.empty else "selected services"
    leading_share = float(top_two.sum() / source.cost_usd.sum() * 100) if source.cost_usd.sum() else 0
    headline = f"{drivers} drive {leading_share:.1f}% of selected cloud spend"
    date_range = f"{frame.billing_date.min():%b %d, %Y} – {frame.billing_date.max():%b %d, %Y} · USD"
    labels = list(historic_labels)
    actual_series: list[dict] = []
    for i, service in enumerate([*top_services, "Other"]):
        if service not in pivot.columns:
            continue
        color = color_for(str(service), i)
        actual_series.append({"name": str(service), "type": "line", "stack": "actual-spend", "smooth": .42,
                              "symbol": "none", "data": (pivot[service].astype(float) / 1000).tolist(),
                              "lineStyle": {"width": 1.5, "color": color},
                              "areaStyle": {"opacity": .68, "color": color}, "emphasis": {"focus": "series"}})
    if grain == "Daily":
        daily_total = source.groupby("period").cost_usd.sum().reindex(historic_labels).astype(float)
        avg = daily_total.rolling(7, min_periods=1).mean()
        actual_series.append({"name": "Daily total · 7-day average", "type": "line", "data": (avg / 1000).tolist(),
                              "smooth": .45, "symbol": "none", "lineStyle": {"color": "#26364A", "width": 3},
                              "z": 8})
        actual_series.append({"name": "Raw daily total", "type": "bar", "data": (daily_total / 1000).tolist(),
                              "barMaxWidth": 5, "itemStyle": {"color": "rgba(8,126,139,.18)"}, "z": 1})

    forecast = next_30_day_forecast(frame)
    forecast_labels: list[str] = []
    fc_rows = pd.DataFrame()
    if not forecast.empty:
        fc = forecast.copy()
        fc["period"] = _period_labels(fc.billing_date, grain)
        fc_rows = fc.groupby("period", as_index=False)[["forecast_usd", "lower_usd", "upper_usd"]].sum()
        forecast_labels = fc_rows.period.astype(str).tolist()
        if grain == "Monthly":
            # The returned horizon is 30 days; keep its future partial month explicit.
            forecast_labels = [f"{label} · forecast" for label in forecast_labels]
        labels.extend(forecast_labels)
    n_hist = len(historic_labels)
    if len(fc_rows):
        anchor = float(totals.iloc[-1]) / 1000
        forecast_values = [None] * (n_hist - 1) + [anchor] + (fc_rows.forecast_usd.astype(float) / 1000).tolist()
        low_values = [None] * (n_hist - 1) + [0.0] + (fc_rows.lower_usd.astype(float) / 1000).tolist()
        band_values = [None] * (n_hist - 1) + [0.0] + ((fc_rows.upper_usd - fc_rows.lower_usd).clip(lower=0).astype(float) / 1000).tolist()
        actual_series.extend([
            {"name": "Expected range · lower", "type": "line", "data": low_values, "stack": "expected-range",
             "symbol": "none", "lineStyle": {"opacity": 0}, "areaStyle": {"opacity": 0}, "tooltip": {"show": False}},
            {"name": "Expected range", "type": "line", "data": band_values, "stack": "expected-range",
             "symbol": "none", "lineStyle": {"opacity": 0}, "areaStyle": {"color": "rgba(78,112,181,.14)"}},
            {"name": "Forecast (next 30 days)", "type": "line", "data": forecast_values,
             "symbol": "circle", "symbolSize": 5, "lineStyle": {"color": "#4E70B5", "width": 2.5, "type": "dashed"},
             "itemStyle": {"color": "#4E70B5"}, "z": 9,
             "markArea": {"silent": True, "itemStyle": {"color": "rgba(78,112,181,.055)"},
                          "data": [[{"xAxis": historic_labels[-1]}, {"xAxis": labels[-1]}]]}},
        ])
    if anomalies is not None and not anomalies.empty:
        alerts = anomalies.copy()
        alerts["period"] = _period_labels(alerts.billing_date, grain)
        alert_data = []
        for row in alerts.itertuples():
            if row.period in historic_labels:
                alert_data.append({"name": f"{row.service} anomaly · +{_money(row.change_usd)}",
                                   "value": [row.period, float(totals.get(row.period, 0)) / 1000],
                                   "itemStyle": {"color": "#C6474D"}})
        if alert_data:
            largest = max(alerts.itertuples(), key=lambda row: row.change_usd)
            largest_label = getattr(largest, "period")
            cause = str(largest.probable_driver or "usage increase")[:28]
            annotation = {"symbol": "circle", "symbolSize": 8, "itemStyle": {"color": "#C6474D"},
                          "label": {"show": True, "position": "top", "distance": 8, "color": "#FFFFFF", "fontSize": 10,
                                    "backgroundColor": "#B83F46", "padding": [6, 8], "borderRadius": 5,
                                    "width": 170, "overflow": "break", "lineHeight": 14,
                                    "formatter": f"{largest.billing_date:%b} {largest.billing_date.day} · {largest.service} +{_money(largest.change_usd)}\\n{cause}"},
                          "data": [{"coord": [largest_label, float(totals.get(largest_label, 0)) / 1000]}]}
            actual_series.append({"name": "Detected anomaly", "type": "scatter", "data": alert_data,
                                  "symbol": "circle", "symbolSize": 10, "itemStyle": {"color": "#C6474D"},
                                  "z": 15, "markPoint": annotation})
    planning = budget_summary(frame)
    budget_value = float(planning.budget_usd.sum()) if not planning.empty else None
    days_factor = {"Daily": 1 / 30, "Weekly": 7 / 30, "Monthly": 1.0}[grain]
    if budget_value:
        budget_k = budget_value * days_factor / 1000
        actual_series.append({"name": "Budget guide", "type": "line", "data": [None] * n_hist + [budget_k] * len(forecast_labels),
                              "symbol": "none", "lineStyle": {"color": "#D29B45", "type": "dashed", "width": 1.7},
                              "markLine": {"symbol": "none", "data": [{"yAxis": budget_k,
                                          "label": {"formatter": "Budget", "position": "insideEndTop"}}]}})
    visible_legend = [str(service) for service in [*top_services, "Other"] if service in pivot.columns]
    if grain == "Daily":
        visible_legend.extend(["Raw daily total", "Daily total · 7-day average"])
    if len(fc_rows):
        visible_legend.extend(["Expected range", "Forecast (next 30 days)"])
    if budget_value:
        visible_legend.append("Budget guide")
    if anomalies is not None and not anomalies.empty:
        visible_legend.append("Detected anomaly")
    option = {"color": PALETTE, "grid": {"left": 72, "right": 26, "top": 24, "bottom": 68, "containLabel": True},
              "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}, "backgroundColor": "#FFF",
                          "borderColor": "#DCE5EA", "textStyle": {"color": "#26364A"}},
              "legend": {"type": "scroll", "data": visible_legend, "bottom": 2, "textStyle": {"fontSize": 12, "color": "#64748B"}},
              "xAxis": {"type": "category", "data": labels, "boundaryGap": grain == "Daily",
                        "axisLabel": {"fontSize": 12, "color": "#64748B", "hideOverlap": True},
                        "axisLine": {"lineStyle": {"color": "#DCE5EA"}}},
              "yAxis": {"type": "value", "name": "USD (thousands)", "axisLabel": {"fontSize": 12, "color": "#64748B",
                                                          "formatter": "${value}K"},
                        "splitLine": {"lineStyle": {"color": "#E8EEF2"}}},
              "series": actual_series}
    return option, headline, date_range


def _service_bar_options(frame: pd.DataFrame):
    totals = frame.groupby("service").cost_usd.sum().sort_values(ascending=False)
    top = totals.head(8).copy()
    other_value = float(totals.iloc[8:].sum()) if len(totals) > 8 else 0.0
    total = float(frame.cost_usd.sum())
    services = top.index.astype(str).tolist()
    values_by_service = {str(name): float(value) for name, value in top.items()}
    services.append("Other")
    values_by_service["Other"] = other_value
    # ECharts' inverse category axis presents the ranked list top-to-bottom;
    # Other remains the final category and is always drawn as the neutral row.
    labels = services
    values = [values_by_service[label] for label in labels]
    shares = [value / total * 100 if total else 0 for value in values]
    movers = _movers_by(frame, "service")
    deltas = movers.set_index("service").change_pct.to_dict() if not movers.empty else {}
    max_value = max(values, default=0.0)
    # Scale the top value close to the plot edge without rounding a small sample
    # up to a disproportionately large $1,000+ interval (which made its bars
    # look like short slivers). Intervals follow the value's order of magnitude.
    axis_step = max(0.01, 10 ** math.floor(math.log10(max_value)) / 10) if max_value > 0 else 1000.0
    axis_max = math.ceil(max_value * 1.04 / axis_step) * axis_step if max_value > 0 else 1000.0
    rows = []
    for index, service in enumerate(labels):
        value = values_by_service[service]
        share = value / total * 100 if total else 0.0
        delta = deltas.get(service)
        usage = (frame.loc[frame.service == service].groupby("usage_type").cost_usd.sum().sort_values(ascending=False)
                 if service != "Other" else pd.Series(dtype=float))
        largest_usage = str(usage.index[0]) if not usage.empty else "Multiple services"
        if service == "Other":
            color = "#B8C2CC"
        elif index == 0:
            color = {"type": "linear", "x": 0, "y": 0, "x2": 1, "y2": 0,
                     "colorStops": [{"offset": 0, "color": "#087E8B"}, {"offset": 1, "color": "#3E9EAA"}]}
        else:
            color = color_for(service, index)
        rich = {"value": {"color": "#26364A", "fontSize": 13, "fontWeight": 650, "padding": [0, 5, 0, 7]},
                "share": {"color": "#718096", "fontSize": 13, "padding": [0, 5, 0, 3]}}
        if delta is None:
            label_text = f"{{value|{format_usd(value, compact=True)}}} {{share|{share:.1f}%}}"
        else:
            tone = "up" if delta > 0 else "down" if delta < 0 else "flat"
            chip_color = "#A6383E" if tone == "up" else "#23734D" if tone == "down" else "#667085"
            rich[tone] = {"color": chip_color, "fontSize": 13, "backgroundColor": "#F4F6F8", "borderRadius": 7, "padding": [3, 6, 3, 6]}
            label_text = f"{{value|{format_usd(value, compact=True)}}} {{share|{share:.1f}%}} {{{tone}|{delta:+.1f}% MoM}}"
        rows.append({"name": service, "value": value, "share": share, "mom_pct": delta,
                     "spend_display": format_usd(value, compact=True),
                     "mom_display": f"{delta:+.1f}%" if delta is not None else "Not available",
                     "usage_type": largest_usage,
                     "itemStyle": {"color": color, "borderRadius": [0, 9, 9, 0]},
                     "silent": service == "Other" and other_value == 0,
                     "label": {"show": True, "position": "right", "distance": 10,
                               "formatter": label_text, "rich": rich, "fontSize": 13}})
    service_totals = top
    top_two = service_totals.head(2)
    leading_share = float(top_two.sum() / total * 100) if total else 0.0
    if top_two.empty:
        title = "Service spend"
    elif len(top_two) == 1:
        title = f"{top_two.index[0]} represents {leading_share:.0f}% of spend"
    else:
        title = f"{top_two.index[0]} and {top_two.index[1]} make up {leading_share:.0f}% of spend"
    months = sorted(frame.billing_month.astype(str).unique())
    period = f"{months[0]} – {months[-1]}" if months else "Selected billing period"
    options = {"grid": {"left": 108, "right": 232, "top": 14, "bottom": 32, "containLabel": True},
               "tooltip": {"trigger": "item", "backgroundColor": "#FFFFFF", "borderColor": "#DCE5EA",
                           "textStyle": {"color": "#26364A", "fontSize": 13}},
               "xAxis": {"type": "value", "min": 0, "max": axis_max, "interval": axis_step,
                         "axisLabel": {"formatter": "${value}", "fontSize": 12, "color": "#64748B"},
                         "splitLine": {"lineStyle": {"color": "#E8EEF2"}},
                         "axisLine": {"show": False}},
               "yAxis": {"type": "category", "data": labels, "inverse": True,
                         "axisLabel": {"fontSize": 13, "color": "#526477", "margin": 13},
                         "axisLine": {"show": False}, "axisTick": {"show": False}},
               "series": [{"name": "Spend", "type": "bar", "data": rows,
                           "barMaxWidth": 25, "barMinHeight": 2, "label": {"show": True},
                           "animationDuration": 800, "animationEasing": "cubicOut"}]}
    options["_row_count"] = len(labels)
    options["_spend_rows"] = rows
    options["_tooltip_js"] = """function(params) {
      const d = params.data || {};
      const esc = v => String(v ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));
      const color = d.mom_pct > 0 ? '#A6383E' : d.mom_pct < 0 ? '#23734D' : '#667085';
      return '<div style=\"min-width:210px;padding:4px 2px;color:#26364A\"><strong>' + esc(params.name) + '</strong>' +
        '<div style=\"margin-top:8px\">Spend <b>' + esc(d.spend_display) + '</b></div>' +
        '<div>Share of total <b>' + Number(d.share || 0).toFixed(1) + '%</b></div>' +
        '<div>Month-over-month <b style=\"color:' + color + '\">' + esc(d.mom_display) + '</b></div>' +
        '<div style=\"margin-top:8px;color:#64748B\">Largest usage type: ' + esc(d.usage_type) + '</div></div>';
    }"""
    return options, title, f"{period} · USD · ranked by selected-period spend"


def _composition_options(frame: pd.DataFrame, dimension: str):
    field = {"Service": "service", "Team": "team", "Account": "account_id", "Region": "region", "Environment": "environment"}[dimension]
    source = frame.copy()
    if field == "team":
        source[field] = source[field].replace("", "Untagged")
    summary = source.groupby(field).cost_usd.sum().sort_values(ascending=False)
    total = float(summary.sum())
    rows = summary.reset_index().rename(columns={field: "Category", "cost_usd": "Spend"})
    rows["Share"] = (rows.Spend / total * 100) if total else 0
    small = rows.Share < 3
    if small.any():
        other = {"Category": "Other", "Spend": float(rows.loc[small, "Spend"].sum()),
                 "Share": float(rows.loc[small, "Share"].sum())}
        rows = pd.concat([rows.loc[~small], pd.DataFrame([other])], ignore_index=True).sort_values("Spend", ascending=False)
    data = [{"name": str(row.Category), "value": float(row.Spend),
             "itemStyle": {"color": color_for(row.Category, i)}}
            for i, row in enumerate(rows.itertuples())]
    option = {"tooltip": {"trigger": "item", "formatter": "{b}<br/>${c} · {d}%"},
              "series": [{"name": "Spend mix", "type": "pie", "radius": ["57%", "78%"], "center": ["50%", "50%"],
                          "data": data, "label": {"show": False}, "labelLine": {"show": False},
                          "emphasis": {"scale": True, "scaleSize": 7}}],
              "graphic": [{"type": "text", "left": "center", "top": "45%",
                           "style": {"text": f"{format_usd(total, compact=True)}\ntotal spend", "textAlign": "center",
                                     "fill": "#26364A", "fontSize": 17, "fontWeight": 700}}]}
    return option, rows, total


def _render_donut_legend(rows: pd.DataFrame) -> None:
    """Display a compact color keyed spend/share legend beside an ECharts donut."""
    from html import escape as html_escape
    st.markdown("<div class='finops-legend'>", unsafe_allow_html=True)
    for i, row in enumerate(rows.itertuples()):
        color = color_for(row.Category, i)
        st.markdown(
            f"<div class='finops-legend-row'><span class='finops-dot' style='background:{color}'></span>"
            f"<span class='finops-legend-name'>{html_escape(str(row.Category))}</span>"
            f"<span class='finops-legend-value'>{format_usd(row.Spend)}<small>{row.Share:.1f}%</small></span></div>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def _anomaly_table(frame):
    if frame.empty:
        st.caption("No anomaly flags available for this selection."); return
    view = frame.copy()
    view["billing_date"] = pd.to_datetime(view.billing_date).dt.strftime("%Y-%m-%d")
    view = view.rename(columns={"billing_date":"Date", "account_id":"Account", "service":"Service", "region":"Region", "observed_usd":"Actual (USD)", "baseline_usd":"Expected (USD)", "expected_low_usd":"Expected low (USD)", "expected_high_usd":"Expected high (USD)", "change_usd":"Impact (USD)", "change_pct":"Deviation (%)", "severity":"Severity", "probable_driver":"Probable usage driver", "scenario":"Injected synthetic cause", "status":"Review status"})
    # Keep the review queue readable at normal laptop widths; detailed context stays
    # available in the anomaly timeline and source data.
    compact = ["Date", "Service", "Actual (USD)", "Expected (USD)", "Impact (USD)", "Deviation (%)", "Severity", "Probable usage driver"]
    view = view[[column for column in compact if column in view.columns]]
    config={col:st.column_config.NumberColumn(format="$%.0f") for col in ["Actual (USD)","Expected (USD)","Expected low (USD)","Expected high (USD)","Impact (USD)"]}
    config["Deviation (%)"]=st.column_config.NumberColumn(format="%.1f%%")
    styled=view.style.map(lambda v:"background-color:#FCE9E8;color:#A12F35;font-weight:700" if v=="High" else "background-color:#FFF3D6;color:#895A08;font-weight:700" if v=="Medium" else "background-color:#E9F1FF;color:#315A9B;font-weight:700",subset=["Severity"])
    st.dataframe(styled, hide_index=True, width="stretch", column_config=config)


def _render_trends(frame):
    st.title("Cost Trends")
    if frame.empty: st.info("No records match the filters."); return
    controls = st.columns([1,1,1], gap="medium")
    grain = controls[0].radio("Time grain", ["Daily", "Weekly", "Monthly"], index=1, horizontal=True, key="trend-grain")
    group_labels = {"Service":"service", "Team":"team", "Account":"account_id", "Region":"region", "Environment":"environment"}
    group_label = controls[1].selectbox("Group by", list(group_labels), key="trend-group")
    display = controls[2].selectbox("Chart style", ["Stacked area", "Stacked bars"], key="trend-style")
    group = group_labels[group_label]
    available_months = sorted(frame.billing_month.astype(str).unique())
    if len(available_months) > 1:
        selected_months = st.select_slider("Billing month range", options=available_months,
                                           value=(available_months[0], available_months[-1]), key="trend-month-range")
        frame = frame[frame.billing_month.astype(str).between(selected_months[0], selected_months[1])].copy()
        st.caption(f"Showing {selected_months[0]} through {selected_months[1]} · {len(frame):,} billing records")
    source = frame.copy()
    source[group] = source[group].replace("", "Untagged")
    if grain == "Daily": source["period"] = source.billing_date
    elif grain == "Weekly": source["period"] = source.billing_date.dt.to_period("W").dt.start_time
    else: source["period"] = source.billing_date.dt.to_period("M").dt.start_time
    totals = source.groupby("period").cost_usd.sum().sort_index()
    table = source.groupby(["period", group], as_index=False).cost_usd.sum()
    top_groups = source.groupby(group).cost_usd.sum().nlargest(5).index
    source[group] = source[group].where(source[group].isin(top_groups), "Other")
    table = source.groupby(["period", group], as_index=False).cost_usd.sum()
    share = table.groupby(group).cost_usd.sum().sort_values(ascending=False)
    latest_total = float(totals.iloc[-1]) if len(totals) else 0.0
    prior_total = float(totals.iloc[-2]) if len(totals) > 1 else None
    latest_period = pd.Timestamp(totals.index[-1]) if len(totals) else None
    last_observed_date = pd.Timestamp(source.billing_date.max())
    if grain == "Weekly" and latest_period is not None:
        period_complete = (latest_period + pd.Timedelta(days=6)).date() <= last_observed_date.date()
    elif grain == "Monthly" and latest_period is not None:
        period_complete = (latest_period + pd.offsets.MonthEnd(0)).date() <= last_observed_date.date()
    else:
        period_complete = True
    delta = ((latest_total / prior_total - 1) * 100) if prior_total and period_complete else None
    takeaway = f"Latest {grain.lower()} total {_money(latest_total)}"
    if delta is not None:
        takeaway += f" ({delta:+.1f}% vs previous period)"
    elif grain == "Weekly" and not period_complete:
        takeaway += " · partial week; comparison omitted"
    chart_type = "area" if display == "Stacked area" else "bar"
    fig = px.area(table, x="period", y="cost_usd", color=group, labels={"period":"Billing period","cost_usd":"Spend (USD)",group:group_label}, title=takeaway) if chart_type == "area" else px.bar(table,x="period",y="cost_usd",color=group,labels={"period":"Billing period","cost_usd":"Spend (USD)",group:group_label},title=takeaway)
    if chart_type == "bar": fig.update_layout(barmode="stack")
    fig.update_yaxes(title="Spend (USD)",tickprefix="$",tickformat="~s")
    fig.update_layout(height=460,hovermode="x unified")
    themed = _chart_theme(fig)
    trend_options = plotly_figure_to_echarts(themed, reduce_motion=bool(st.session_state.get("reduce_motion", False)))
    spike_flags = anomaly_events(frame)
    if not spike_flags.empty and trend_options.get("series"):
        spike = spike_flags.sort_values("change_usd", ascending=False).iloc[0]
        spike_period = _period_labels(pd.Series([spike.billing_date]), grain).iloc[0]
        period_totals = source.assign(_period=_period_labels(source.billing_date, grain)).groupby("_period").cost_usd.sum()
        category_label = pd.to_datetime(spike_period).strftime("%b %d")
        if spike_period in period_totals.index:
            trend_options["series"][0]["markPoint"] = {
                "symbol": "circle", "symbolSize": 10, "itemStyle": {"color": "#C6474D"},
                "data": [{"coord": [category_label, float(period_totals.loc[spike_period])],
                          "label": {"show": True, "position": "top", "distance": 8,
                                    "color": "#FFFFFF", "fontSize": 10,
                                    "backgroundColor": "#B83F46", "padding": [6, 8], "borderRadius": 5,
                                    "width": 175, "overflow": "break", "lineHeight": 14,
                                    "formatter": f"{spike.billing_date:%b %d} · {spike.service} +{_money(spike.change_usd)}\\n{spike.probable_driver}"}}]}
    render_echarts_card(trend_options, takeaway,
                        subtitle=f"{selected_months[0]} – {selected_months[1]} · {grain.lower()} · {group_label} · USD" if len(available_months)>1 else f"{grain} · {group_label} · USD",
                        help_text="Stacked colors show the five largest categories and Other. Hover a period to compare category contribution; the largest rule-based spike includes its synthetic usage driver where available.",
                        key="cost-trends-stacked", height=460)
    if not spike_flags.empty:
        largest_flag=spike_flags.sort_values("change_usd",ascending=False).iloc[0]
        st.caption(f"Review action · Validate {largest_flag.probable_driver} for {largest_flag.service}; modeled increase {_money(largest_flag.change_usd)} on {largest_flag.billing_date:%b %d}. Confirm workload context before changing capacity.")
    if grain == "Daily":
        st.caption("Shared date, service, account, region, team, and environment filters apply to this chart and the mover table.")
    playback_source = frame.copy()
    playback_source["month"] = playback_source.billing_date.dt.to_period("M").astype(str)
    playback = playback_source.groupby(["month", group], as_index=False).cost_usd.sum()
    playback_months = sorted(playback.month.unique())
    playback_groups = playback.groupby(group).cost_usd.sum().nlargest(8).index.tolist()
    playback_options = []
    for month in playback_months:
        month_rows = playback[playback.month == month].set_index(group).cost_usd
        playback_options.append({"title": {"text": month},
                                 "series": [{"name": "Monthly spend", "type": "bar", "barMaxWidth": 52,
                                             "data": [{"value": float(month_rows.get(name, 0)),
                                                       "itemStyle": {"color": color_for(str(name), i)},
                                                       "label": {"show": True, "position": "top",
                                                                 "formatter": format_usd(float(month_rows.get(name, 0)))}}
                                                      for i, name in enumerate(playback_groups)],
                                             "itemStyle": {"borderRadius": [7, 7, 0, 0]},
                                             "label": {"show": True, "position": "top", "fontSize": 11}}]})
    if playback_months:
        playback_spec = {"baseOption": {"timeline": {"axisType": "category", "autoPlay": False,
                                                       "playInterval": 1100, "data": playback_months,
                                                       "controlStyle": {"color": "#087E8B", "borderColor": "#087E8B"},
                                                       "label": {"fontSize": 12, "color": "#64748B"}},
                                        "tooltip": {"trigger": "axis"},
                                        "grid": {"left": 65, "right": 26, "top": 45, "bottom": 88, "containLabel": True},
                                        "xAxis": {"type": "category", "data": [str(name) for name in playback_groups],
                                                  "axisLabel": {"fontSize": 12, "rotate": 18}},
                                        "yAxis": {"type": "value", "axisLabel": {"formatter": "${value}"},
                                                  "splitLine": {"lineStyle": {"color": "#E8EEF2"}}},
                                        "series": [{"type": "bar"}]}, "options": playback_options}
        render_echarts_card(playback_spec, "Monthly spend playback",
                            subtitle="Use Play or drag the month slider to compare service shares across billing months.",
                            help_text="Each frame shows monthly cost by selected grouping. This is a timeline of the currently selected data, not a forecast.",
                            key="trend-month-playback", height=460)
    st.subheader("Top movers vs previous represented month")
    mover_dimension = st.selectbox("Mover dimension", ["Service", "Usage type"], key="mover-dimension")
    mover_field = "service" if mover_dimension == "Service" else "usage_type"
    movers = _movers_by(frame,mover_field)
    if movers.empty:
        st.info("Two represented months are required for a month-over-month mover comparison.")
    else:
        mover_display = movers.rename(columns={mover_field:mover_dimension,"previous_usd":"Previous month","current_usd":"Current month","change_usd":"Change (USD)","change_pct":"Change (%)"})
        st.dataframe(mover_display.style.map(lambda v:"color:#A12F35;font-weight:600" if isinstance(v,(int,float)) and v>0 else "color:#087E8B;font-weight:600" if isinstance(v,(int,float)) and v<0 else "",subset=["Change (USD)","Change (%)"]),hide_index=True,width="stretch",column_config={"Previous month":st.column_config.NumberColumn(format="$%.0f"),"Current month":st.column_config.NumberColumn(format="$%.0f"),"Change (USD)":st.column_config.NumberColumn(format="$%.0f"),"Change (%)":st.column_config.NumberColumn(format="%.1f%%")})


def _render_breakdown(frame):
    st.title("Service Breakdown")
    if frame.empty: st.info("No records match the filters."); return
    dimensions = [("Service","service"),("Account","account_id"),("Region","region"),("Team","team"),("Environment","environment")]
    tabs = st.tabs([label for label,_ in dimensions])
    for tab,(label,field) in zip(tabs,dimensions):
        with tab:
            data=frame.copy(); data[field]=data[field].replace("","Untagged")
            summary=_dimension_summary(data,field)
            all_spend=data.groupby(field).cost_usd.sum().sort_values(ascending=False)
            total=float(all_spend.sum()) if not all_spend.empty else 0
            share=float(all_spend.iloc[0]/total*100) if total and not all_spend.empty else 0
            top=str(all_spend.index[0]) if not all_spend.empty else "No category"
            display_spend=all_spend.head(8).copy()
            if len(all_spend)>8: display_spend.loc["Other"] = float(all_spend.iloc[8:].sum())
            movers=_movers_by(data,field)
            delta_by=movers.set_index(field).change_pct.to_dict() if not movers.empty else {}
            display_rows=[]
            for i,(name,value) in enumerate(display_spend.sort_values().items()):
                pct=float(value/total*100) if total else 0
                label_text=f"{format_usd(value,compact=True)} · {pct:.1f}%"
                delta=delta_by.get(name)
                if delta is not None and pd.notna(delta): label_text+=f" · {delta:+.0f}% MoM"
                color=color_for(str(name),i) if field=="service" else PALETTE[i%len(PALETTE)]
                rgb = tuple(int(color[pos:pos+2], 16) for pos in (1, 3, 5))
                lighter = "#" + "".join(f"{round(channel + (255-channel)*.28):02X}" for channel in rgb)
                gradient = {"type":"linear","x":0,"y":0,"x2":1,"y2":0,"colorStops":[{"offset":0,"color":lighter},{"offset":1,"color":color}]}
                display_rows.append({"name":str(name),"value":float(value),"itemStyle":{"color":gradient,"borderRadius":[0,7,7,0]},
                                     "label":{"show":True,"position":"right","formatter":label_text,"fontSize":11,"color":"#526477"}})
            tick_step = max(1000, math.ceil(float(display_spend.max()) / 3000) * 1000) if len(display_spend) else 1000
            tick_max = math.ceil(float(display_spend.max()) / tick_step) * tick_step if len(display_spend) else 1000
            bar_option={"grid":{"left":120,"right":150,"top":16,"bottom":30,"containLabel":True},
                        "tooltip":{"trigger":"axis","axisPointer":{"type":"shadow"},"backgroundColor":"#FFF","borderColor":"#DCE5EA"},
                        "xAxis":{"type":"value","axisLabel":{"formatter":"${value}"},"splitLine":{"lineStyle":{"color":"#E8EEF2"}}},
                        "yAxis":{"type":"category","data":[r["name"] for r in display_rows],"axisLabel":{"fontSize":12}},
                        "series":[{"name":"Spend","type":"bar","data":display_rows,"barMaxWidth":24,"label":{"show":True}}]}
            bar_option["xAxis"].update({"name":"Spend (USD)","nameLocation":"middle","nameGap":38,"max":tick_max,"interval":tick_step})
            comp_label="Environment" if field=="environment" else label
            donut_option,donut_rows,donut_total=_composition_options(data,comp_label)
            bar_col,donut_col=st.columns([1.15,1],gap="medium")
            with bar_col:
                render_echarts_card(bar_option,f"{top} leads {label.lower()} spend at {share:.1f}%",
                                    subtitle=f"Top eight {label.lower()} categories plus Other · {format_usd(total)} total",
                                    help_text="Outside labels include spend, share, and recent MoM change where available.",
                                    key=f"breakdown-bar-{field}",height=400)
                if not all_spend.empty:
                    st.caption(f"Review action · {top} accounts for {_money(all_spend.iloc[0])}; validate its recent usage and allocation before prioritizing a cost change.")
            with donut_col:
                render_echarts_card(donut_option,f"Spend mix by {label.lower()}",
                                    subtitle=f"{format_usd(donut_total)} selected spend",
                                    help_text="Hover a slice for category spend and share. Categories below 3% are grouped as Other.",
                                    key=f"breakdown-donut-{field}",height=360)
                _render_donut_legend(donut_rows)
            visible=summary.rename(columns={field:label,"spend":"Spend (USD)","share_pct":"Share of total","change_usd":"MoM change (USD)","trend":"Recent trend"})
            st.dataframe(visible,hide_index=True,width="stretch",column_config={"Spend (USD)":st.column_config.NumberColumn(format="$%.0f"),"Share of total":st.column_config.ProgressColumn(format="%.1f%%",min_value=0,max_value=100),"MoM change (USD)":st.column_config.NumberColumn(format="$%.0f"),"Recent trend":st.column_config.LineChartColumn("Recent trend")})
    st.subheader("Untagged spend")
    untagged=frame[(frame.team=="")|(frame.project=="")|(frame.environment=="")]
    missing_cost=float(untagged.cost_usd.sum()); total=float(frame.cost_usd.sum())
    a,b=st.columns(2)
    a.metric("Spend on records with incomplete tags",_money(missing_cost),f"{missing_cost/total:.1%} of selected spend" if total else None)
    b.caption("A record is incomplete when team, project, or environment is blank. No missing tag is imputed.")
    service_totals=allocation_summary(untagged,"service").rename(columns={"service":"AWS service","cost_usd":"Spend (USD)"})
    account_totals=allocation_summary(untagged,"account_id").rename(columns={"account_id":"Account","cost_usd":"Spend (USD)"})
    left,right=st.columns(2)
    left.dataframe(service_totals,hide_index=True,width="stretch",column_config={"Spend (USD)":st.column_config.NumberColumn(format="$%.0f")})
    right.dataframe(account_totals,hide_index=True,width="stretch",column_config={"Spend (USD)":st.column_config.NumberColumn(format="$%.0f")})
    st.download_button("Download incomplete-tag line items",untagged.to_csv(index=False).encode("utf-8"),file_name="incomplete_tag_line_items.csv",mime="text/csv")


def _dimension_summary(frame: pd.DataFrame, field: str) -> pd.DataFrame:
    """Provide spend share, month movement, and sparkline values by dimension."""
    monthly = frame.groupby(["billing_month",field],as_index=False).cost_usd.sum()
    months=sorted(monthly.billing_month.unique())
    pivot=monthly.pivot(index=field,columns="billing_month",values="cost_usd").fillna(0)
    current=months[-1] if months else None; previous=months[-2] if len(months)>1 else None
    result=pd.DataFrame({field:pivot.index,"spend":pivot[current].values if current else 0})
    total=float(result.spend.sum()) if not result.empty else 0
    result["share_pct"]=result.spend/total*100 if total else 0
    result["change_usd"]=pivot[current].values-pivot[previous].values if previous else None
    result["trend"]=[pivot.loc[name,months[-6:]].astype(float).tolist() for name in pivot.index]
    return result.sort_values("spend",ascending=False).reset_index(drop=True)


def _status_path() -> Path:
    """Return the ignored local path used for persisted UI review statuses."""
    return PROJECT_ROOT / "data" / "user" / "review_status.json"


def _anomaly_status_key(row) -> str:
    """Stable key shared by the overview and anomaly review controls."""
    return f"anomaly|{row.account_id}|{row.service}|{row.region}|{row.billing_date:%Y-%m-%d}"


def _load_review_status() -> dict[str, str]:
    path = _status_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_review_status(values: dict[str, str]) -> None:
    """Persist review metadata locally without touching billing data."""
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2), encoding="utf-8")


def _render_anomalies(frame):
    st.title("Anomaly Review"); st.caption("Weekday-adjusted rule-based flags; validate each alert with workload context.")
    st.markdown("**Method:** compare each daily account/service/region value with up to four previous observations from the same weekday. Flag when actual exceeds 2.5× baseline and is at least $10 higher. Expected range is baseline ±2 standard deviations.")
    anomalies=anomaly_events(frame)
    if anomalies.empty: st.success("No baseline flags found for this selection.")
    statuses=_load_review_status()
    keys=[_anomaly_status_key(r) for r in anomalies.itertuples()]
    anomalies=anomalies.copy(); anomalies["status"]=[statuses.get(key,"Open") for key in keys]
    counts=anomalies.severity.value_counts(); a,b,c,d=st.columns(4); a.metric("Open flags",int((anomalies.status=="Open").sum())); b.metric("High",int(counts.get("High",0))); c.metric("Medium",int(counts.get("Medium",0))); d.metric("Low",int(counts.get("Low",0)))
    truth=load_ground_truth()
    if not truth.empty:
        truth=truth[(truth.billing_date>=frame.billing_date.min())&(truth.billing_date<=frame.billing_date.max())]
        truth=truth[truth.account_id.isin(frame.account_id.unique())&truth.service.isin(frame.service.unique())&truth.region.isin(frame.region.unique())]
        quality=detection_quality(anomalies,truth)
        q1,q2,q3=st.columns(3); q1.metric("Synthetic precision",f"{quality['precision']:.1%}"); q2.metric("Synthetic recall",f"{quality['recall']:.1%}"); q3.metric("Synthetic F1",f"{quality['f1']:.1%}")
        st.caption("Quality is measured against generated ground truth in the selected window, not real-world detection performance.")
    if not anomalies.empty:
        st.subheader("Review queue")
        _anomaly_table(anomalies)
        largest=anomalies.sort_values("change_usd",ascending=False).iloc[0]
        st.info(f"Review action · {largest.service} has a modeled increase of {_money(largest.change_usd)} on {largest.billing_date:%Y-%m-%d}. Check the workload and deployment context for {largest.probable_driver} before changing resources.")
        selected=st.selectbox("Update review status",anomalies.index,format_func=lambda i:f"{anomalies.loc[i,'service']} · {anomalies.loc[i,'billing_date']:%Y-%m-%d}")
        status=st.selectbox("Status",["Open","Acknowledged","Resolved"])
        if st.button("Save status",key="save-anomaly-status"):
            statuses[keys[selected]]=status; _save_review_status(statuses); st.success("Review status saved locally.")
    if frame.empty: return
    st.subheader("Anomaly timeline")
    service_options=sorted(frame.service.unique())
    default_service=anomalies.sort_values("change_usd",ascending=False).iloc[0].service if not anomalies.empty else service_options[0]
    service=st.selectbox("Timeline service",service_options,index=service_options.index(default_service),key="anomaly-service")
    scoped=frame[frame.service==service]
    account_options=sorted(scoped.account_id.unique())
    default_account=anomalies[anomalies.service==service].sort_values("change_usd",ascending=False).iloc[0].account_id if not anomalies[anomalies.service==service].empty else account_options[0]
    account=st.selectbox("Timeline account",account_options,index=account_options.index(default_account),key="anomaly-account")
    timeline=scoped[scoped.account_id==account].groupby("billing_date",as_index=False).cost_usd.sum().sort_values("billing_date")
    series=timeline.set_index("billing_date").cost_usd.copy(); baseline=series.copy(); spread=series.copy()*0
    for day in range(7):
        mask=series.index.day_of_week==day; values=series.loc[mask]
        baseline.loc[mask]=values.rolling(4,min_periods=3).mean().shift(1)
        spread.loc[mask]=values.rolling(4,min_periods=3).std().shift(1).fillna(0)
    timeline=timeline.set_index("billing_date")
    fig=go.Figure()
    fig.add_trace(go.Scatter(x=timeline.index,y=(baseline+2*spread).values,name="Expected upper range",line={"width":0},showlegend=False))
    fig.add_trace(go.Scatter(x=timeline.index,y=(baseline-2*spread).clip(lower=0).values,name="Expected range",line={"width":0},fill="tonexty",fillcolor="rgba(78,112,181,.13)"))
    fig.add_trace(go.Scatter(x=timeline.index,y=timeline.cost_usd,name="Actual spend",line={"color":ACCENT,"width":2}))
    selected_flags=anomalies[(anomalies.service==service)&(anomalies.account_id==account)]
    if not selected_flags.empty: fig.add_trace(go.Scatter(x=selected_flags.billing_date,y=selected_flags.observed_usd,name="Rule-based alert",mode="markers",marker={"color":"#B64449","size":9}))
    fig.update_layout(title=f"{service} actual and weekday baseline · {_money(timeline.cost_usd.sum())} total",height=370)
    fig.update_yaxes(title="Spend (USD)",tickprefix="$",tickformat="~s")
    render_chart(_chart_theme(fig), key="chart-" + str(abs(hash(fig.layout.title.text or "chart"))))


def _render_optimization(frame):
    st.title("Optimization Opportunities"); st.caption("Simulated findings only. No resources are modified and all amounts are estimates.")
    findings=simulated_optimization_findings(frame)
    if findings.empty: st.info("No simulated resource findings are available for this data source."); return
    statuses=_load_review_status()
    findings=findings.copy()
    findings["status"]=[statuses.get(f"resource|{row.resource_id}",row.status) for row in findings.itertuples()]
    monthly=float(pd.to_numeric(findings.estimated_savings_usd).sum()); annual=monthly*12
    k1,k2=st.columns(2); k1.metric("Estimated monthly savings",_money(monthly)); k2.metric("Estimated annual run-rate",_money(annual),help="Monthly estimate multiplied by 12; not realized savings.")
    findings["estimated_savings_usd"]=pd.to_numeric(findings.estimated_savings_usd)
    category=findings.groupby("category",as_index=False).estimated_savings_usd.sum()
    top=category.sort_values("estimated_savings_usd",ascending=False).iloc[0]
    fig=px.bar(category.sort_values("estimated_savings_usd"),x="estimated_savings_usd",y="category",orientation="h",labels={"estimated_savings_usd":"Estimated savings (USD/month)","category":"Category"},title=f"{top.category} leads simulated opportunity estimates at {_money(top.estimated_savings_usd)}/month")
    fig.update_xaxes(tickprefix="$",tickformat="~s"); render_chart(_chart_theme(fig), key="chart-" + str(abs(hash(fig.layout.title.text or "chart"))))
    human=findings.rename(columns={"billing_month":"Billing month","resource_id":"Resource ID","account_id":"Account","region":"Region","service":"AWS service","team":"Team","environment":"Environment","finding":"Finding","current_monthly_cost_usd":"Current monthly cost","estimated_savings_usd":"Estimated savings / month","utilization_pct":"Utilization (%)","evidence":"Evidence","recommended_action":"Recommended action","confidence":"Confidence","status":"Status"})
    action_summaries={
        "Stop outside approved operating hours or right-size.": "Stop outside approved hours or right-size.",
        "Review instance class and non-production schedule.": "Review instance class and non-prod schedule.",
        "Snapshot if needed, then review removal.": "Snapshot if needed; confirm before removal.",
        "Release after owner confirms it is unused.": "Release after owner confirms unused.",
        "Review lifecycle transitions and retention requirements.": "Review lifecycle and retention policy.",
        "Review target health and consolidate if appropriate.": "Review target health; consolidate if safe.",
    }
    if "Recommended action" in human:
        human["Recommended action"] = human["Recommended action"].replace(action_summaries)
    visible_columns=["Resource ID","AWS service","Finding","Current monthly cost","Estimated savings / month","Recommended action","Status"]
    human=human[[column for column in visible_columns if column in human.columns]]
    edited=st.data_editor(human,hide_index=True,width="stretch",disabled=[c for c in human.columns if c!="Status"],column_config={"Current monthly cost":st.column_config.NumberColumn(format="$%.0f"),"Estimated savings / month":st.column_config.NumberColumn(format="$%.0f"),"Utilization (%)":st.column_config.NumberColumn(format="%.1f%%"),"Status":st.column_config.SelectboxColumn(options=["Open","Accepted","Ignored"])})
    changed={f"resource|{row['Resource ID']}":row["Status"] for _,row in edited.iterrows()}
    if st.button("Save finding statuses",key="save-resource-status"):
        statuses.update(changed); _save_review_status(statuses); st.success("Finding statuses saved locally.")
    coverage=commitment_coverage_frame()
    if not coverage.empty:
        coverage=coverage[coverage.account_id.isin(frame.account_id.unique())] if not frame.empty else coverage
        st.subheader("Commitment coverage and utilization · synthetic")
        means=coverage.groupby("commitment_type")[["coverage_pct","utilization_pct"]].mean(numeric_only=True)
        cols=st.columns(max(1,len(means)))
        for col,(kind,values) in zip(cols,means.iterrows()):
            col.metric(f"{kind} coverage",f"{values.coverage_pct:.1f}%",f"{values.utilization_pct:.1f}% utilization")
        gauge_series=[]
        for i,(kind,values) in enumerate(means.iterrows()):
            for j,(metric,value,color,bands) in enumerate([
                ("Coverage",float(values.coverage_pct),"#087E8B",[[.65,"#D29B45"],[.85,"#3E9EAA"],[1,"#087E8B"]]),
                ("Utilization",float(values.utilization_pct),"#4E70B5",[[.65,"#087E8B"],[.85,"#D29B45"],[1,"#C6474D"]]),
            ]):
                x=15+23*(i*2+j)
                gauge_series.append({"name":f"{kind} {metric}","type":"gauge","center":[f"{x}%","65%"],
                                     "radius":"28%","startAngle":180,"endAngle":0,"min":0,"max":100,
                                     "pointer":{"show":False},"progress":{"show":True,"width":12,"itemStyle":{"color":color}},
                                     "axisLine":{"lineStyle":{"width":12,"color":bands}},
                                     "axisTick":{"show":False},"splitLine":{"show":False},"axisLabel":{"show":False},
                                     "title":{"show":True,"offsetCenter":[0,"-118%"],"fontSize":12,"color":"#526477"},
                                     "detail":{"valueAnimation":True,"formatter":"{value}%","offsetCenter":[0,"32%"],"fontSize":15,"color":"#26364A"},
                                     "data":[{"value":round(value,1),"name":f"{kind} {metric}"}]})
        if gauge_series:
            render_echarts_card({"series":gauge_series,"tooltip":{"trigger":"item"}},
                                "Commitment coverage and utilization",
                                subtitle="Synthetic Savings Plan and Reserved Instance indicators · percentage scale",
                                help_text="Coverage represents estimated eligible usage covered by commitments. Utilization compares modeled commitment usage with purchased capacity. These are generated example indicators.",
                                key="optimization-commitment-gauges",height=335)
        low=means[means.coverage_pct<65]
        advice=(f"Coverage is comparatively low for {', '.join(low.index)} in this synthetic scenario; model commitment fit against steady eligible usage before considering a purchase." if not low.empty else "The simulated coverage is above the example threshold; review utilization and workload stability before drawing conclusions.")
        st.info(advice)


def _render_budgets(frame):
    st.title("Budgets & Forecasts"); st.caption("Synthetic planning budgets and modeled forecasts; these are not AWS account budgets.")
    if frame.empty: st.info("No records match the filters."); return
    fc=forecast_month_end(frame); st.metric(f"{fc['month']} month-end estimate",_money(fc["forecast"]),f"MTD actual {_money(fc['actual'])}")
    st.info("Month-end uses month-to-date cost divided by elapsed calendar days, projected to month end. The 30-day series uses a linear trend with weekday averages and a residual-based 95% band; uncertainty is approximate and not seasonality-validated.")
    st.subheader("Monthly budget and forecast by team")
    budget=budget_summary(frame)
    if budget.empty:
        st.info("No matching planning budget is available for this data mode. Synthetic budgets are shown only with the synthetic demo dataset.")
    else:
        budget=budget.copy()
        budget["projected_utilization_pct"]=budget.forecast_usd/budget.budget_usd*100
        budget["status"]=budget.projected_utilization_pct.map(lambda x:"No budget" if pd.isna(x) else "Over" if x>=100 else "At risk" if x>=85 else "On track")
        ranked=budget.dropna(subset=["projected_utilization_pct"]).sort_values("projected_utilization_pct",ascending=False)
        if not ranked.empty:
            highest=ranked.iloc[0]
            st.subheader(f"{highest.team} is projected at {highest.projected_utilization_pct:.1f}% of budget")
            difference=float(highest.budget_usd-highest.forecast_usd)
            if difference >= 0:
                st.info(f"Budget review · {_money(difference)} projected headroom for {highest.team}. Validate the run-rate and monitor discretionary usage through month end.")
            else:
                st.info(f"Budget review · {_money(abs(difference))} projected overrun for {highest.team}. Review the largest flexible workload and confirm whether planned growth is expected.")
        else:
            st.subheader("No team budgets are available for this selection")
        visible=budget.rename(columns={"team":"Team","budget_usd":"Monthly budget","actual_usd":"Month-to-date spend","utilization_pct":"Budget consumed","forecast_usd":"Projected month-end","projected_utilization_pct":"Projected budget use","overrun_usd":"Estimated overrun","status":"Status"})
        st.dataframe(visible,hide_index=True,width="stretch",column_config={"Monthly budget":st.column_config.NumberColumn(format="$%.0f"),"Month-to-date spend":st.column_config.NumberColumn(format="$%.0f"),"Budget consumed":st.column_config.ProgressColumn(format="%.1f%%",min_value=0,max_value=100),"Projected month-end":st.column_config.NumberColumn(format="$%.0f"),"Projected budget use":st.column_config.ProgressColumn(format="%.1f%%",min_value=0,max_value=150),"Estimated overrun":st.column_config.NumberColumn(format="$%.0f")})
        risk_rows=ranked.sort_values("projected_utilization_pct")
        risk_colors=["#C6474D" if v>=100 else "#D29B45" if v>=85 else "#23836D" for v in risk_rows.projected_utilization_pct]
        risk_options={"grid":{"left":110,"right":95,"top":18,"bottom":38,"containLabel":True},
                      "tooltip":{"trigger":"axis","axisPointer":{"type":"shadow"},"backgroundColor":"#FFF","borderColor":"#DCE5EA"},
                      "xAxis":{"type":"value","name":"Budget use (%)","nameLocation":"middle","nameGap":30,"max":max(110,float(risk_rows.projected_utilization_pct.max())*1.12 if not risk_rows.empty else 110),
                               "axisLabel":{"formatter":"{value}%"},"splitLine":{"lineStyle":{"color":"#E8EEF2"}}},
                      "yAxis":{"type":"category","data":risk_rows.team.astype(str).tolist(),"axisLabel":{"fontSize":12}},
                      "series":[{"name":"Projected budget use","type":"bar","barMaxWidth":26,
                                 "data":[{"value":float(row.projected_utilization_pct),"itemStyle":{"color":risk_colors[i],"borderRadius":[0,7,7,0]},
                                          "label":{"show":True,"position":"right","formatter":f"{row.projected_utilization_pct:.1f}% · {_money(row.forecast_usd)}","fontSize":11,"color":"#526477"}}
                                         for i,row in enumerate(risk_rows.itertuples())],
                                 "markLine":{"symbol":"none","data":[{"xAxis":85,"lineStyle":{"color":"#D29B45","type":"dashed"},"label":{"formatter":"At risk · 85%"}},
                                                                          {"xAxis":100,"lineStyle":{"color":"#C6474D","type":"dashed"},"label":{"formatter":"Budget limit"}}]}}]}
        render_echarts_card(risk_options,"Projected budget risk by team",
                            subtitle="Forecast month-end utilization · green <85%, amber 85–99%, red ≥100%",
                            help_text="Bar color reflects the projected month-end share of each planning budget. This is a synthetic planning signal, not an AWS account budget.",
                            key="budget-risk-bars",height=400)
    forecast=next_30_day_forecast(frame)
    if not forecast.empty:
        st.subheader("Next 30 days · modeled daily total")
        forecast_dates=forecast.billing_date.dt.strftime("%b %d").tolist()
        forecast_options={"grid":{"left":72,"right":26,"top":18,"bottom":68,"containLabel":True},
                          "tooltip":{"trigger":"axis","backgroundColor":"#FFF","borderColor":"#DCE5EA"},
                          "legend":{"data":["Expected range","Forecast"],"bottom":2,"textStyle":{"fontSize":12}},
                          "xAxis":{"type":"category","data":forecast_dates,"axisLabel":{"fontSize":11,"hideOverlap":True},"boundaryGap":False},
                          "yAxis":{"type":"value","name":"Daily spend (USD)","axisLabel":{"formatter":"${value}"},"splitLine":{"lineStyle":{"color":"#E8EEF2"}}},
                          "series":[{"name":"Expected range · lower","type":"line","data":forecast.lower_usd.astype(float).tolist(),"stack":"confidence","symbol":"none","lineStyle":{"opacity":0},"areaStyle":{"opacity":0},"tooltip":{"show":False}},
                                    {"name":"Expected range","type":"line","data":(forecast.upper_usd-forecast.lower_usd).clip(lower=0).astype(float).tolist(),"stack":"confidence","symbol":"none","lineStyle":{"opacity":0},"areaStyle":{"color":"rgba(78,112,181,.16)"}},
                                    {"name":"Forecast","type":"line","data":forecast.forecast_usd.astype(float).tolist(),"symbol":"none","smooth":.25,"lineStyle":{"color":"#087E8B","width":2.5,"type":"dashed"},"itemStyle":{"color":"#087E8B"}}]}
        render_echarts_card(forecast_options,f"Modeled next-30-day total: {_money(forecast.forecast_usd.sum())}",
                            subtitle="Weekday profile plus linear trend · shaded approximate 95% range",
                            help_text="Daily forecast uses the existing linear-trend and weekday-profile model. The range is residual-based and is not seasonality-validated.",
                            key="budget-30day-forecast",height=380)


def _render_ask_ai(frame):
    st.title("Ask AI")
    st.caption("Ask questions about the selected cost data. Answers are computed from these records, and the query used is shown.")
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.getenv("OPENAI_API_KEY", "")
    try:
        if not api_key:
            api_key = str(st.secrets.get("OPENAI_API_KEY", ""))
    except Exception:
        # Streamlit raises when no secrets file exists; never expose that detail.
        api_key = ""
    llm_enabled = os.getenv("ASK_AI_LLM_ENABLED", "").casefold() == "true"
    use_llm = False
    if api_key and llm_enabled:
        use_llm = st.checkbox("Use configured AI query generation (may incur API charges)", value=False,
                              help="Sends the question to the configured model for SQL generation. Billing rows stay local; generated SQL is validated before execution.")
        if not use_llm:
            st.caption("Offline mode: using built-in queries")
    else:
        st.caption("Offline mode: using built-in queries")
    examples = ["Why did spend move last month?", "Which team has the most untagged spend?",
                "Which service costs the most?", "What anomaly should I review first?"]
    st.subheader("Suggested questions")
    for offset in range(0, len(examples), 2):
        chips = st.columns(2)
        for col, question in zip(chips, examples[offset:offset + 2]):
            if col.button(question, key="suggest-" + str(examples.index(question)), use_container_width=True):
                st.session_state["pending_finops_question"] = question
    if "finops_chat" not in st.session_state:
        st.session_state.finops_chat = []
    clear_col, hint_col = st.columns([.18, 1], vertical_alignment="center")
    if clear_col.button("Clear chat", key="clear-finops-chat", disabled=not st.session_state.finops_chat):
        st.session_state.finops_chat = []
        st.session_state.pop("finops_last_intent", None)
        st.rerun()
    hint_col.caption("Built-in questions use fixed, parameterized SELECT queries on a read-only in-memory SQLite copy of the selected rows.")
    prompt = st.chat_input("Ask about the selected billing data")
    prompt = prompt or st.session_state.pop("pending_finops_question", None)
    if prompt:
        st.session_state.finops_chat.append({"role": "user", "content": prompt})
        with st.spinner("Checking the selected cost data…"):
            generated_sql = None
            if use_llm and api_key:
                try:
                    generated_sql = generate_sql_with_openai(prompt, api_key)
                except Exception:
                    # Network/provider errors can include request context; keep them private.
                    st.session_state.finops_chat.append({"role": "assistant", "content": "I couldn't reach the configured query-generation service. No query was run; try again in Offline mode.", "sql": None, "rows": []})
            if not (use_llm and api_key and generated_sql is None):
                result = run_question(prompt, frame,
                                      previous_intent=st.session_state.get("finops_last_intent"),
                                      generated_sql=generated_sql)
                st.session_state.finops_last_intent = result.get("intent")
                st.session_state.finops_chat.append({"role": "assistant", **result})
        st.session_state["finops_fresh_answer_index"] = len(st.session_state.finops_chat) - 1
    for index, message in enumerate(st.session_state.finops_chat):
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.write(message["content"])
                continue
            if index == st.session_state.get("finops_fresh_answer_index") and message.get("answer"):
                st.write_stream(_stream_answer(message["answer"]))
                st.session_state.pop("finops_fresh_answer_index", None)
            else:
                st.write(message.get("answer", message.get("content", "")))
            if message.get("rows"):
                if message.get("title"):
                    st.caption(message["title"])
                st.dataframe(_ask_ai_display_table(message["rows"]), hide_index=True, width="stretch")
            if message.get("sql"):
                with st.expander("How I got this"):
                    st.caption("This SELECT ran against the selected rows in a temporary, read-only, in-memory SQLite database." if message.get("executed") else "The query was not run. It was unsupported, rejected by the safety checks, or could not execute; no database or cloud resources were changed.")
                    st.code(message["sql"], language="sql")
                    if message.get("params"):
                        st.caption(f"Bound parameters: {message['params']}")


def _stream_answer(answer: str):
    """Yield small text chunks for Streamlit's chat typing animation."""
    words = answer.split(" ")
    for index, word in enumerate(words):
        yield word + (" " if index < len(words) - 1 else "")


def _ask_ai_display_table(rows: list[dict]) -> pd.DataFrame:
    """Give assistant result tables readable headings and shared USD formatting."""
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    money_columns = {"spend", "cost_usd", "current_spend", "previous_spend", "change_usd",
                     "observed_usd", "baseline_usd", "estimated_savings_usd", "current_monthly_cost_usd",
                     "budget_usd", "actual_usd", "forecast_usd", "overrun_usd", "month_to_date_usd",
                     "projected_month_end_usd"}
    percent_columns = {"change_pct", "utilization_pct"}
    for column in result.columns:
        if column in money_columns:
            result[column] = result[column].map(lambda value: format_usd(value) if pd.notna(value) else "—")
        elif column in percent_columns:
            result[column] = result[column].map(lambda value: f"{float(value):+.1f}%" if pd.notna(value) else "—")
        elif column == "row_count":
            result[column] = result[column].map(lambda value: f"{int(value):,}" if pd.notna(value) else "—")
    headings = {"service": "AWS service", "billing_date": "Billing date", "billing_month": "Billing month",
                "region": "Region", "environment": "Environment", "team": "Team", "team_name": "Team",
                "account_id": "Account", "usage_type": "Usage type", "resource_id": "Resource ID",
                "resource_state": "Resource state", "finding": "Finding", "severity": "Severity",
                "probable_driver": "Probable driver", "recommended_action": "Recommended action",
                "evidence": "Evidence", "week": "Week", "spend": "Spend",
                "previous_spend": "Previous spend", "current_spend": "Current spend", "change_usd": "Change (USD)",
                "change_pct": "Change (%)", "cost_usd": "Cost (USD)", "observed_usd": "Observed (USD)",
                "baseline_usd": "Baseline (USD)", "estimated_savings_usd": "Estimated savings / month",
                "current_monthly_cost_usd": "Current monthly cost", "budget_usd": "Illustrative budget",
                "actual_usd": "Actual spend", "utilization_pct": "Budget used (%)", "forecast_usd": "Forecast",
                "overrun_usd": "Projected overrun", "row_count": "Records", "month_to_date_usd": "Spend to date",
                "elapsed_days": "Days elapsed", "days_in_month": "Days in month", "projected_month_end_usd": "Month-end projection"}
    return result.rename(columns={column: headings.get(column, column.replace("_", " ").title()) for column in result.columns})


def _render_records(frame):
    st.title("Billing Records"); st.caption("Search and download the currently filtered dataset. Synthetic records are clearly labeled.")
    if frame.empty: st.info("No records match the selected filters."); return
    term=st.text_input("Search records",placeholder="Service, account, region, tag, resource…")
    view=frame.copy()
    if term:
        mask=view.astype(str).apply(lambda col:col.str.contains(term,case=False,regex=False)).any(axis=1); view=view[mask]
    page_size=st.selectbox("Rows per page",[25,50,100],index=0)
    pages=max(1,(len(view)+page_size-1)//page_size); page=st.number_input("Page",min_value=1,max_value=pages,value=1,step=1)
    st.caption(f"{len(view):,} matching records · page {page} of {pages}")
    friendly={"record_id":"Record ID","billing_date":"Billing date","billing_month":"Billing month","account_id":"Account","region":"Region","service":"AWS service","usage_type":"Usage type","resource_id":"Resource ID","resource_state":"Resource state","cost_usd":"Cost (USD)","team":"Team","project":"Project","environment":"Environment","scenario":"Synthetic scenario","dataset_note":"Data provenance"}
    display=view.rename(columns=friendly)
    visible_columns=["Billing date","Billing month","Account","Region","AWS service","Cost (USD)","Team","Project","Environment"]
    page_view=display.iloc[(page-1)*page_size:page*page_size]
    st.dataframe(page_view[[column for column in visible_columns if column in page_view.columns]],hide_index=True,width="stretch",column_config={"Cost (USD)":st.column_config.NumberColumn(format="$%.2f"),"Billing date":st.column_config.DateColumn(format="YYYY-MM-DD")})
    st.caption("The download contains all available fields; the table shows the core billing and allocation columns for readability.")
    st.download_button("Download filtered CSV",display.to_csv(index=False).encode("utf-8"),file_name="billing_records_filtered.csv",mime="text/csv")


def main():
    st.set_page_config(page_title="Cloud Cost Analytics",page_icon=":material/cloud:",layout="wide")
    page,filters,mode,frame=_sidebar()
    if not frame.empty:
        frame=apply_filters(frame,filters)
    if mode == "Local MySQL" and not frame.empty:
        st.info("The existing MySQL schema stores monthly billing rows only. Daily resource trends, allocation tags, and resource-level optimization findings are unavailable in this source; blank dimensions are left unassigned.")
    renderers={"Overview":_render_overview,"Cost Trends":_render_trends,"Service Breakdown":_render_breakdown,"Anomalies":_render_anomalies,"Optimization":_render_optimization,"Budgets & Forecasts":_render_budgets,"Ask AI":_render_ask_ai,"Billing Records":_render_records}
    if page=="Overview":
        renderers[page](frame,mode)
    else:
        renderers[page](frame)
