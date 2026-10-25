"""Responsive local dashboard for fictional sample or local MySQL data."""

from decimal import Decimal
from dataclasses import asdict
from datetime import datetime
from inspect import signature

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from .dashboard_data import (
    build_dashboard_data,
    load_mysql_records,
    load_sample_records,
    monthly_totals,
    region_totals,
    service_totals,
    summarize_spending,
)


def _money(value: Decimal | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


def _frame(rows: list[dict[str, str]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if "total_cost_usd" in frame:
        frame["total_cost_usd"] = pd.to_numeric(frame["total_cost_usd"])
    return frame


def _stretch_width_kwargs(function) -> dict[str, str | bool]:
    """Use the current Streamlit width API, with compatibility for older versions."""
    return {"width": "stretch"} if "width" in signature(function).parameters else {"use_container_width": True}


def _monthly_chart(frame: pd.DataFrame) -> alt.Chart:
    """Build a labeled trend chart with a shared light chart theme."""
    return (
        alt.Chart(frame)
        .mark_line(color="#087E8B", strokeWidth=3, point=alt.OverlayMarkDef(filled=True, size=70))
        .encode(
            x=alt.X("billing_month:N", title="Billing month", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("total_cost_usd:Q", title="Spend (USD)", axis=alt.Axis(format="$,.0f", grid=True)),
            tooltip=[
                alt.Tooltip("billing_month:N", title="Billing month"),
                alt.Tooltip("total_cost_usd:Q", title="Spend", format="$,.2f"),
            ],
        )
        .properties(height=320)
        .configure(background="#FFFFFF")
        .configure_axis(labelColor="#34445B", titleColor="#24364B", gridColor="#E7EDF4", domainColor="#BBC7D4")
        .configure_view(stroke=None)
        .configure(font="Segoe UI")
    )


def _breakdown_chart(frame: pd.DataFrame, category: str, category_title: str) -> alt.Chart:
    """Build a readable horizontal bar chart with currency axes and tooltips."""
    return (
        alt.Chart(frame)
        .mark_bar(color="#4361A5", cornerRadiusEnd=4)
        .encode(
            x=alt.X("total_cost_usd:Q", title="Spend (USD)", axis=alt.Axis(format="$,.0f", grid=True)),
            y=alt.Y(f"{category}:N", title=category_title, sort="-x"),
            tooltip=[
                alt.Tooltip(f"{category}:N", title=category_title),
                alt.Tooltip("total_cost_usd:Q", title="Spend", format="$,.2f"),
            ],
        )
        .properties(height=max(260, min(420, 48 * len(frame))))
        .configure(background="#FFFFFF")
        .configure_axis(labelColor="#34445B", titleColor="#24364B", gridColor="#E7EDF4", domainColor="#BBC7D4")
        .configure_view(stroke=None)
        .configure(font="Segoe UI")
    )


def _anomaly_table(rows: list[dict[str, str | None]]) -> pd.DataFrame:
    """Format only fields produced by the anomaly rule for display."""
    frame = pd.DataFrame(
        [
            {
                "Service": row["service"],
                "Billing Month": row["billing_month"],
                "Current Cost": float(Decimal(row["current_cost_usd"])),
                "Baseline": float(Decimal(row["baseline_cost_usd"])),
                "Absolute Change": float(Decimal(row["increase_usd"])),
                "Change (%)": float(Decimal(row["percentage_change"])) if row["percentage_change"] is not None else None,
            }
            for row in rows
        ]
    )
    return frame


def _records_table(records) -> pd.DataFrame:
    """Create a display-only table from the validated records already in memory."""
    frame = pd.DataFrame([asdict(record) for record in records])
    if "cost_usd" in frame:
        frame["cost_usd"] = pd.to_numeric(frame["cost_usd"].map(float))
    return frame


def _render_monthly_trend(monthly: pd.DataFrame) -> None:
    st.subheader("Monthly spending trend")
    if monthly.empty:
        st.caption("No monthly totals are available for this period.")
        return
    st.altair_chart(_monthly_chart(monthly), theme=None, **_stretch_width_kwargs(st.altair_chart))


def _render_service_breakdown(services: pd.DataFrame, regions: pd.DataFrame) -> None:
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.subheader("Spend by AWS service")
        if services.empty:
            st.caption("No service totals are available for this period.")
        else:
            st.altair_chart(
                _breakdown_chart(services, "service", "AWS service"),
                theme=None,
                **_stretch_width_kwargs(st.altair_chart),
            )
    with right:
        st.subheader("Spend by AWS region")
        if regions.empty:
            st.caption("No regional totals are available for this period.")
        else:
            st.altair_chart(
                _breakdown_chart(regions, "region", "AWS region"),
                theme=None,
                **_stretch_width_kwargs(st.altair_chart),
            )


def _render_anomalies(anomalies: list[dict[str, str | None]]) -> None:
    st.subheader("Anomaly review")
    st.caption(
        "Rule: current service cost > 1.5× the mean of up to three preceding calendar months and at least $100 above baseline. "
        "Severity is not assigned by this rule; every flag needs human review."
    )
    if not anomalies:
        st.success("No anomaly flags were detected in the selected billing period.")
        return
    anomaly_data = _anomaly_table(anomalies)
    st.dataframe(
        anomaly_data,
        hide_index=True,
        **_stretch_width_kwargs(st.dataframe),
        column_config={
            "Service": st.column_config.TextColumn("Service", width="medium"),
            "Billing Month": st.column_config.TextColumn("Billing Month", width="small"),
            "Current Cost": st.column_config.NumberColumn("Current Cost", format="$%.2f"),
            "Baseline": st.column_config.NumberColumn("Baseline", format="$%.2f"),
            "Absolute Change": st.column_config.NumberColumn("Absolute Change", format="$%.2f"),
            "Change (%)": st.column_config.NumberColumn("Change (%)", format="%.2f%%"),
        },
    )


def main() -> None:
    """Launch the expanded, distinct-page FinOps dashboard."""
    from .finops_app import main as finops_main
    finops_main()
