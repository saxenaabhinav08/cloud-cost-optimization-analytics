"""Shared light FinOps UI and chart styling helpers."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st
from .charts import PALETTE

PRIMARY = "#087E8B"
INK = "#26364A"
MUTED = "#65758A"


def inject_styles() -> None:
    """Apply a restrained light theme to dashboard cards and navigation."""
    reduce_motion = bool(st.session_state.get("reduce_motion", False))
    animation = "" if reduce_motion else """
    @keyframes finops-enter { from {opacity:0; transform:translateY(5px)} to {opacity:1; transform:translateY(0)} }
    .main .block-container { animation:finops-enter 300ms ease-out both; }
    """
    style = """<style>
    :root { color-scheme: light; }
    [data-testid="stAppViewContainer"] { background:#F8FAFB; color:#26364A; }
    [data-testid="stHeader"] { background:rgba(248,250,251,.94); }
    [data-testid="stSidebar"] { background:#F1F5F6; border-right:1px solid #E0E7EA; width:min(16rem,82vw)!important; min-width:min(16rem,82vw)!important; }
    .block-container { padding:1.35rem 2rem 2.8rem; max-width:1520px; }
    h1,h2,h3 { color:#26364A; letter-spacing:-.025em; }
    h1 { font-size:1.85rem!important; font-weight:720!important; }
    h2 { font-size:1.28rem!important; font-weight:680!important; }
    h3 { font-size:1.05rem!important; font-weight:650!important; }
    p, label, [data-testid="stCaptionContainer"] { color:#586B7E; }
    [data-testid="stMetric"] { border:1px solid #E1E8EC; border-radius:12px; padding:.85rem .95rem; background:#FFFFFF; box-shadow:0 2px 8px rgba(38,54,74,.035); min-height:128px; }
    [data-testid="stMetricLabel"] p { color:#526477!important; font-weight:620!important; font-size:.8rem!important; }
    [data-testid="stMetricValue"] div { color:#26364A!important; font-size:clamp(1.15rem,1.45vw,1.65rem)!important; font-weight:720!important; }
    [data-testid="stMetricDelta"] { font-size:.76rem!important; }
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap:.43rem; }
    [data-testid="stSidebar"] button[kind="primary"] { background:#DDF0EE!important; border:1px solid #C5E3E0!important; color:#155E63!important; box-shadow:inset 3px 0 #0B8790!important; justify-content:flex-start!important; text-align:left!important; }
    [data-testid="stSidebar"] button[kind="tertiary"] { color:#44566A!important; justify-content:flex-start!important; text-align:left!important; }
    [data-testid="stSidebar"] button[kind="primary"] div, [data-testid="stSidebar"] button[kind="tertiary"] div { justify-content:flex-start!important; }
    .finops-card { border:1px solid #E1E8EC; border-radius:12px; background:#FFF; padding:1rem 1.1rem; margin:.15rem 0 .5rem; min-height:115px; }
    .finops-card h4 { color:#26364A; font-size:.98rem; line-height:1.4; margin:0 0 .4rem; }
    .finops-card p { color:#526477; font-size:.85rem; line-height:1.5; margin:.2rem 0; }
    .finops-impact { color:#087E8B!important; font-weight:700; }
    .finops-legend { display:flex; flex-direction:column; gap:.4rem; padding:.5rem .15rem; }
    .finops-legend-row { display:grid; grid-template-columns:10px minmax(78px,1fr) auto; align-items:center; gap:.5rem; border-bottom:1px solid #EDF1F3; padding:.35rem 0; color:#34465A; font-size:.78rem; line-height:1.25; }
    .finops-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
    .finops-legend-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .finops-legend-value { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; font-weight:650; }
    .finops-legend-value small { display:block; color:#718096; font-weight:500; font-size:.7rem; }
    """ + animation + """
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important;scroll-behavior:auto!important;}}
    .severity-high { color:#A12F35!important; background:#FCE9E8; padding:.15rem .45rem; border-radius:99px; font-size:.73rem; font-weight:700; }
    .severity-medium { color:#895A08!important; background:#FFF3D6; padding:.15rem .45rem; border-radius:99px; font-size:.73rem; font-weight:700; }
    .severity-low { color:#315A9B!important; background:#E9F1FF; padding:.15rem .45rem; border-radius:99px; font-size:.73rem; font-weight:700; }
    [data-testid="stAlert"] { border-radius:10px; }
    @media(max-width:900px){ .block-container{padding:1rem 1rem 2rem;} [data-testid="stMetric"]{min-height:112px;} }
    </style>"""
    st.markdown(style, unsafe_allow_html=True)


def finops_template(fig: go.Figure, *, title: str | None = None, height: int | None = None) -> go.Figure:
    """Normalize every chart to a white accessible style and clean hover labels."""
    fig.update_layout(
        template="plotly_white", title=title or fig.layout.title.text,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"family":"Inter, Segoe UI, Arial, sans-serif", "size":13, "color":INK},
        margin={"l":18,"r":20,"t":64,"b":24},
        legend={"orientation":"h","y":1.12,"title_text":""},
        colorway=PALETTE,
        hoverlabel={"bgcolor":"#FFFFFF","bordercolor":"#D9E2E8","font":{"size":13,"color":INK}},
    )
    if height is not None:
        fig.update_layout(height=height)
    fig.update_xaxes(showgrid=False, zeroline=False, automargin=True)
    fig.update_yaxes(gridcolor="#E7EDF2", zeroline=False, automargin=True)
    return fig
