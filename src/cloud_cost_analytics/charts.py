"""Shared ECharts conversion and rendering helpers for the FinOps dashboard."""
from __future__ import annotations

import base64
from datetime import date
import json
import math
from typing import Any

import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder
import numpy as np
import streamlit as st
from .formatting import format_usd

PALETTE = ["#087E8B", "#3E9EAA", "#4E70B5", "#7388C5", "#5A9D8D", "#79B9AD", "#8874A8", "#A7B4C7"]
INK = "#26364A"
MUTED = "#64748B"
SERVICE_COLORS = {
    "Amazon EC2": PALETTE[0], "Amazon S3": PALETTE[1], "Amazon RDS": PALETTE[2],
    "Amazon CloudWatch": PALETTE[3], "AWS Lambda": PALETTE[4],
    "Amazon VPC": PALETTE[5], "AWS Data Transfer": PALETTE[6],
    "Amazon EKS": "#7056A8", "Amazon ECS": "#2B7885",
    "Amazon ElastiCache": "#9C83B5", "AWS Backup": "#4E8D7C",
    "Amazon DynamoDB": "#A3AFBC", "Amazon CloudFront": "#6B90B8",
    "Amazon EBS": "#2B7885", "Data Transfer": "#8874A8",
    "Elastic Load Balancing": "#4E8D7C", "NAT Gateway": "#79B9AD",
    "Other": "#B9C6D2",
}


def color_for(name: str, index: int = 0) -> str:
    """Return a stable service color, falling back to the shared category palette."""
    return SERVICE_COLORS.get(str(name), PALETTE[index % len(PALETTE)])


def plotly_figure_to_echarts(fig: go.Figure, *, reduce_motion: bool = False) -> dict[str, Any]:
    """Convert the existing Plotly figure descriptions to native ECharts options.

    This keeps existing page calculations intact while making ECharts the renderer.
    The converter intentionally handles the trace types used in this application.
    """
    source = json.loads(json.dumps(fig.to_plotly_json(), cls=PlotlyJSONEncoder))

    def expand_typed_arrays(value: Any) -> Any:
        """Plotly 6 JSON encodes numpy arrays as base64 buffers; ECharts needs lists."""
        if isinstance(value, dict):
            if "bdata" in value and "dtype" in value:
                raw = base64.b64decode(value["bdata"])
                return expand_typed_arrays(np.frombuffer(raw, dtype=np.dtype(value["dtype"])).tolist())
            return {key: expand_typed_arrays(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand_typed_arrays(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    source = expand_typed_arrays(source)
    layout = source.get("layout", {})
    traces = source.get("data", [])
    series: list[dict[str, Any]] = []
    categories: list[Any] = []
    for index, trace in enumerate(traces):
        kind = trace.get("type", "scatter")
        name = trace.get("name") or f"Series {index + 1}"
        color = color_for(name, index)
        trace_marker = trace.get("marker") or {}
        trace_line = trace.get("line") or {}
        color = color_for(name, index) if name else (trace_marker.get("color") or trace_line.get("color") or PALETTE[index % len(PALETTE)])
        if isinstance(color, list):
            color = PALETTE[index % len(PALETTE)]
        x = trace.get("x") or []
        y = trace.get("y") or []
        if kind == "treemap":
            data = [{"name": str(label), "value": value, "itemStyle": {"color": PALETTE[i % len(PALETTE)]}}
                    for i, (label, value) in enumerate(zip(trace.get("labels", []), trace.get("values", [])))]
            series.append({"type": "treemap", "name": name, "data": data, "roam": False,
                           "label": {"show": True, "fontSize": 13, "color": INK}})
            continue
        if kind == "pie":
            labels = trace.get("labels", [])
            values = trace.get("values", [])
            series.append({"type": "pie", "name": name, "radius": ["48%", "74%"], "center": ["50%", "50%"],
                           "data": [{"name": str(label), "value": value, "itemStyle": {"color": PALETTE[i % len(PALETTE)]}}
                                    for i, (label, value) in enumerate(zip(labels, values))],
                           "label": {"show": False}, "emphasis": {"scale": True, "scaleSize": 6}})
            continue
        horizontal_bar = kind == "bar" and trace.get("orientation") == "h"
        if not categories and (x or y):
            category_values = y if horizontal_bar else x
            categories = []
            for value in category_values:
                raw = str(value)
                try:
                    raw = date.fromisoformat(raw[:10]).strftime("%b %d")
                except ValueError:
                    pass
                categories.append(raw)
        is_bar = kind == "bar"
        mode = trace.get("mode", "lines")
        is_scatter = kind == "scatter" and "markers" in mode and "lines" not in mode
        item: dict[str, Any] = {"name": name, "type": "bar" if is_bar else "scatter" if is_scatter else "line",
                                "data": x if horizontal_bar else y, "itemStyle": {"color": color}, "lineStyle": {"color": color, "width": 2},
                                "symbol": "circle" if is_scatter else "none", "symbolSize": 8,
                                "smooth": bool(trace.get("line", {}).get("shape") == "spline"),
                                "connectNulls": True}
        if trace.get("stackgroup") or trace.get("fill") in ("tonexty", "tozeroy"):
            item.update({"stack": trace.get("stackgroup", "spend"), "areaStyle": {"opacity": .52}})
        if is_bar:
            item.update({"barMaxWidth": 34, "stack": "spend" if layout.get("barmode") == "stack" else None,
                         "itemStyle": {"color": color, "borderRadius": [0, 5, 5, 0] if horizontal_bar else [5, 5, 0, 0]}})
        if trace.get("line", {}).get("dash") == "dash":
            item["lineStyle"]["type"] = "dashed"
        if trace.get("showlegend") is False:
            item["silent"] = True
        if (trace.get("marker") or {}).get("color") == "#B64449":
            item["itemStyle"]["color"] = "#C6474D"
            item["symbolSize"] = 11
        series.append(item)

    xaxis = layout.get("xaxis", {})
    yaxis = layout.get("yaxis", {})
    option: dict[str, Any] = {
        "backgroundColor": "#FFFFFF",
        "animation": not reduce_motion,
        "animationDuration": 900,
        "animationDurationUpdate": 650,
        "animationEasing": "cubicOut",
        "animationEasingUpdate": "cubicOut",
        "color": PALETTE,
        "textStyle": {"fontFamily": "Inter, Segoe UI, Arial, sans-serif", "fontSize": 13, "color": INK},
        "tooltip": {"trigger": "axis" if not any(s["type"] == "pie" for s in series) else "item",
                    "backgroundColor": "#FFFFFF", "borderColor": "#DCE5EA", "textStyle": {"color": INK},
                    "axisPointer": {"type": "line"}},
        "legend": {"show": len(series) > 1, "type": "scroll", "bottom": 0, "textStyle": {"color": MUTED, "fontSize": 13}},
        "grid": {"left": 72, "right": 30, "top": 22, "bottom": 62, "containLabel": True},
        "series": series,
    }
    horizontal_bar = any(trace.get("type") == "bar" and trace.get("orientation") == "h" for trace in traces)
    if horizontal_bar and categories:
        fmt = xaxis.get("tickformat", "")
        currency = bool(xaxis.get("tickprefix") == "$" or "$" in fmt)
        option["xAxis"] = {"type": "value", "name": xaxis.get("title", {}).get("text", ""),
                           "nameLocation": "middle", "nameGap": 30,
                           "axisLabel": {"color": MUTED, "fontSize": 12, "formatter": ("${value}" if currency else "{value}")},
                           "splitLine": {"lineStyle": {"color": "#E8EEF2"}}}
        option["yAxis"] = {"type": "category", "data": categories,
                           "axisLabel": {"color": MUTED, "fontSize": 12, "hideOverlap": True},
                           "axisLine": {"lineStyle": {"color": "#DCE5EA"}}}
    elif categories and not any(s["type"] in ("pie", "treemap") for s in series):
        option["xAxis"] = {"type": "category", "data": categories, "name": xaxis.get("title", {}).get("text", ""),
                           "axisLabel": {"color": MUTED, "fontSize": 12, "hideOverlap": True},
                           "axisLine": {"lineStyle": {"color": "#DCE5EA"}}, "boundaryGap": any(s["type"] == "bar" for s in series)}
        fmt = yaxis.get("tickformat", "")
        currency = bool(yaxis.get("tickprefix") == "$" or "$" in fmt)
        option["yAxis"] = {"type": "value", "name": yaxis.get("title", {}).get("text", ""),
                           "axisLabel": {"color": MUTED, "fontSize": 12, "formatter": ("${value}" if currency else "{value}")},
                           "splitLine": {"lineStyle": {"color": "#E8EEF2"}}, "axisLine": {"show": False}}
    return option


def render_chart(fig: go.Figure, *, subtitle: str = "Calculated from the selected billing records.",
                 help_text: str = "Hover over marks to inspect values.", key: str | None = None,
                 height: int | None = None) -> None:
    """Render a consistently framed chart card with native Streamlit ECharts."""
    title = str(fig.layout.title.text or "Cloud spend chart")
    with st.container(border=True):
        left, right = st.columns([1, 0.08], vertical_alignment="top")
        left.markdown(f"**{title}**")
        left.caption(subtitle)
        right.button("Info", key=f"chart-help-{key or title}", help=help_text, type="tertiary")
        options = plotly_figure_to_echarts(fig, reduce_motion=bool(st.session_state.get("reduce_motion", False)))
        chart_height = height or int(fig.layout.height or 360)
        st.echarts_chart(options, height=chart_height, key=key or f"echart-{abs(hash(title))}")


def render_echarts_card(options: dict[str, Any], title: str, *, subtitle: str,
                        help_text: str, key: str, height: int = 390) -> None:
    """Render a native ECharts option in the shared light chart-card treatment."""
    with st.container(border=True):
        title_col, info_col = st.columns([1, .08], vertical_alignment="top")
        title_col.markdown(f"**{title}**")
        title_col.caption(subtitle)
        info_col.button("?", key=f"chart-help-{key}", help=help_text, type="tertiary")
        options = dict(options)
        reduce_motion = bool(st.session_state.get("reduce_motion", False))
        options.update({"backgroundColor": "#FFFFFF", "animation": not reduce_motion,
                        "animationDuration": 850, "animationDurationUpdate": 600,
                        "animationEasing": "cubicOut", "animationEasingUpdate": "cubicOut"})
        st.echarts_chart(options, height=height, key=key)
