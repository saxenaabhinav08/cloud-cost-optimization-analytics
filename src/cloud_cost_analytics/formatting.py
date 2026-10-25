"""Shared display formatting for cloud cost values."""
from __future__ import annotations


def format_usd(value: float, *, compact: bool = False, decimals: int = 0) -> str:
    """Format USD consistently; use cents only when a detailed record needs them."""
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if compact and value >= 1_000_000:
        return f"{sign}${value / 1_000_000:.1f}M"
    if compact and value >= 1_000:
        return f"{sign}${value / 1_000:.1f}K"
    return f"{sign}${value:,.{max(0, int(decimals))}f}"
