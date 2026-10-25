"""Reusable analytics for the explicitly synthetic FinOps dashboard."""
from __future__ import annotations

from pathlib import Path
from io import BytesIO
import pandas as pd
import numpy as np

from .synthetic_data import DEFAULT_OUTPUT, generate_synthetic_billing
from .formatting import format_usd


def synthetic_frame(path: str | Path | None = None) -> pd.DataFrame:
    """Load the generated daily file, generating it on first use if absent."""
    source = Path(path) if path else DEFAULT_OUTPUT
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(generate_synthetic_billing()).to_csv(source, index=False)
    frame = pd.read_csv(source, keep_default_na=False)
    frame["billing_date"] = pd.to_datetime(frame["billing_date"], errors="raise")
    frame["cost_usd"] = pd.to_numeric(frame["cost_usd"], errors="raise")
    frame["billing_month"] = frame["billing_date"].dt.to_period("M").astype(str)
    return frame


def load_cur_csv(content: bytes) -> pd.DataFrame:
    """Validate a canonical or common AWS CUR CSV upload without persisting it."""
    frame = pd.read_csv(BytesIO(content))
    aliases = {
        "line_item_usage_start_date": "billing_date",
        "line_item_usage_account_id": "account_id",
        "product_region": "region", "product_region_code": "region",
        "product_servicecode": "service", "line_item_product_code": "service",
        "line_item_usage_type": "usage_type",
        "line_item_unblended_cost": "cost_usd", "line_item_net_unblended_cost": "cost_usd",
        "resource_tags_user_team": "team", "resource_tags_user_project": "project",
        "resource_tags_user_environment": "environment",
    }
    for alias, canonical in aliases.items():
        if canonical not in frame.columns and alias in frame.columns:
            frame = frame.rename(columns={alias: canonical})
    required = ["billing_date", "account_id", "region", "service", "usage_type", "cost_usd"]
    missing = [name for name in required if name not in frame.columns]
    if missing:
        expected = ", ".join(required)
        raise ValueError(f"CSV is missing required billing columns: {', '.join(missing)}. Expected headers include: {expected}.")
    frame["billing_date"] = pd.to_datetime(frame.billing_date, errors="coerce", utc=True).dt.tz_localize(None)
    frame["cost_usd"] = pd.to_numeric(frame.cost_usd, errors="coerce")
    if frame.billing_date.isna().any():
        raise ValueError("CSV has invalid billing dates; use ISO dates or AWS CUR usage start timestamps.")
    if frame.cost_usd.isna().any() or (frame.cost_usd < 0).any():
        raise ValueError("CSV costs must be numeric and non-negative.")
    for col in required[1:-1] + ["team", "project", "environment"]:
        if col not in frame:
            frame[col] = ""
        frame[col] = frame[col].fillna("").astype(str)
    frame["region"] = frame.region.replace("", "global")
    if "resource_id" not in frame:
        frame["resource_id"] = ""
    frame["resource_state"] = "uploaded"
    frame["scenario"] = ""
    frame["dataset_note"] = "User-uploaded CUR CSV · not synthetic"
    frame["record_id"] = [f"uploaded-{i}" for i in range(len(frame))]
    frame["billing_month"] = frame.billing_date.dt.to_period("M").astype(str)
    return frame


def load_ground_truth(path: str | Path | None = None) -> pd.DataFrame:
    source = Path(path) if path else DEFAULT_OUTPUT.parent / "ground_truth_anomalies.csv"
    if not source.exists():
        return pd.DataFrame(columns=["billing_date", "account_id", "service", "region", "scenario", "cause"])
    frame = pd.read_csv(source)
    frame["billing_date"] = pd.to_datetime(frame.billing_date)
    return frame


def detection_quality(detections: pd.DataFrame, ground_truth: pd.DataFrame) -> dict[str, float | int]:
    """Compute exact date/account/service/region precision, recall, and F1."""
    keys = ["billing_date", "account_id", "service", "region"]
    if detections.empty or ground_truth.empty:
        tp, fp, fn = 0, len(detections), len(ground_truth)
    else:
        detected = set(tuple(row) for row in detections[keys].assign(billing_date=pd.to_datetime(detections.billing_date).dt.strftime("%Y-%m-%d")).itertuples(index=False, name=None))
        actual = set(tuple(row) for row in ground_truth[keys].assign(billing_date=pd.to_datetime(ground_truth.billing_date).dt.strftime("%Y-%m-%d")).itertuples(index=False, name=None))
        tp, fp, fn = len(detected & actual), len(detected - actual), len(actual - detected)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def next_30_day_forecast(frame: pd.DataFrame) -> pd.DataFrame:
    """Linear trend plus weekday seasonal average and a simple residual band."""
    columns = ["billing_date", "forecast_usd", "lower_usd", "upper_usd"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    daily = frame.groupby("billing_date").cost_usd.sum().sort_index()
    if len(daily) < 7:
        return pd.DataFrame(columns=columns)
    x = np.arange(len(daily), dtype=float)
    slope, intercept = np.polyfit(x, daily.to_numpy(), 1)
    residual = daily.to_numpy() - (slope * x + intercept)
    spread = max(float(np.std(residual)), float(daily.mean()) * .05)
    weekday = daily.groupby(daily.index.day_of_week).mean()
    baseline = float(daily.mean())
    dates = pd.date_range(daily.index.max() + pd.Timedelta(days=1), periods=30, freq="D")
    values = [max(0.0, (slope * (len(daily) + i) + intercept) * (weekday[d.weekday()] / baseline if baseline else 1)) for i, d in enumerate(dates)]
    return pd.DataFrame({"billing_date": dates, "forecast_usd": values,
                         "lower_usd": [max(0.0, v - 1.96 * spread) for v in values],
                         "upper_usd": [v + 1.96 * spread for v in values]})


def normalize_mysql_records(records) -> pd.DataFrame:
    """Adapt legacy monthly MySQL records to dashboard columns without inventing tags."""
    if not records:
        return pd.DataFrame(columns=["record_id", "billing_date", "billing_month", "account_id", "region", "service", "usage_type", "resource_id", "resource_state", "cost_usd", "team", "project", "environment", "scenario", "dataset_note"])
    rows = []
    for i, record in enumerate(records):
        rows.append({"record_id": f"mysql-{i}", "billing_date": pd.Timestamp(f"{record.billing_month}-01"),
                     "billing_month": record.billing_month, "account_id": record.account_id, "region": record.region,
                     "service": record.service, "usage_type": record.usage_type, "resource_id": "", "resource_state": "unknown",
                     "cost_usd": float(record.cost_usd), "team": "", "project": "", "environment": "",
                     "scenario": "", "dataset_note": record.dataset_note})
    return pd.DataFrame(rows)


def apply_filters(frame: pd.DataFrame, filters: dict[str, object]) -> pd.DataFrame:
    """Apply inclusive date and optional categorical filters; blanks remain untagged."""
    out = frame.copy()
    if out.empty:
        return out
    if filters.get("start") is not None:
        out = out[out.billing_date >= pd.Timestamp(filters["start"])]
    if filters.get("end") is not None:
        out = out[out.billing_date <= pd.Timestamp(filters["end"])]
    for field in ("service", "account_id", "region", "team", "environment"):
        values = filters.get(field)
        if values:
            out = out[out[field].isin(values)]
    return out.reset_index(drop=True)


def monthly_spend(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["billing_month", "cost_usd"])
    return frame.groupby("billing_month", as_index=False).cost_usd.sum().sort_values("billing_month")


def anomaly_events(frame: pd.DataFrame, multiplier: float = 2.5, minimum_delta: float = 10.0) -> pd.DataFrame:
    """Flag unusually high daily spend using the prior four same-weekday values."""
    columns = ["billing_date", "account_id", "service", "region", "observed_usd", "baseline_usd", "expected_low_usd", "expected_high_usd", "change_usd", "change_pct", "severity", "probable_driver", "scenario"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    grouped = frame.groupby(["account_id", "service", "region", "billing_date"], as_index=False).agg(
        cost_usd=("cost_usd", "sum"), scenario=("scenario", lambda x: ", ".join(v for v in x if v)),
        usage_type=("usage_type", "first"))
    result = []
    for (account, service, region), rows in grouped.groupby(["account_id", "service", "region"]):
        rows = rows.sort_values("billing_date").set_index("billing_date")
        # Compare each date to prior observations with the same weekday to reduce
        # the recurring weekend/weekday cycle. The current point is excluded.
        weekdays = rows.index.day_of_week
        baseline = pd.Series(index=rows.index, dtype=float)
        spread = pd.Series(index=rows.index, dtype=float)
        for weekday in range(7):
            mask = weekdays == weekday
            values = rows.loc[mask, "cost_usd"]
            baseline.loc[mask] = values.rolling(4, min_periods=3).mean().shift(1)
            spread.loc[mask] = values.rolling(4, min_periods=3).std().shift(1)
        for dt, row in rows.iterrows():
            base = baseline.loc[dt]
            change = row.cost_usd - base if pd.notna(base) else 0
            if pd.notna(base) and base > 0 and row.cost_usd > base * multiplier and change >= minimum_delta:
                pct = change / base * 100
                sigma = spread.loc[dt] if pd.notna(spread.loc[dt]) else 0.0
                scenario = row.scenario
                usage = row.usage_type
                severity = "High" if change >= 100 and pct >= 300 else "Medium" if change >= 40 and pct >= 150 else "Low"
                result.append({"billing_date": dt, "account_id": account, "service": service, "region": region,
                               "observed_usd": row.cost_usd, "baseline_usd": base,
                               "expected_low_usd": max(0.0, base - 2 * sigma), "expected_high_usd": base + 2 * sigma,
                               "change_usd": change, "change_pct": pct, "severity": severity,
                               "probable_driver": usage, "scenario": scenario})
    return pd.DataFrame(result, columns=columns).sort_values("change_usd", ascending=False).reset_index(drop=True)


def forecast_month_end(frame: pd.DataFrame) -> dict[str, float | str | None]:
    """Straight-line month-end estimate from month-to-date daily average."""
    if frame.empty:
        return {"month": None, "actual": 0.0, "forecast": 0.0, "days_elapsed": 0, "days_in_month": 0}
    month = frame.billing_date.max().to_period("M")
    current = frame[frame.billing_date.dt.to_period("M") == month]
    days_elapsed = int(current.billing_date.dt.day.max())
    days_in_month = int(month.days_in_month)
    actual = float(current.cost_usd.sum())
    return {"month": str(month), "actual": actual, "forecast": actual / days_elapsed * days_in_month,
            "days_elapsed": days_elapsed, "days_in_month": days_in_month}


def allocation_summary(frame: pd.DataFrame, field: str) -> pd.DataFrame:
    """Summarize tagged spend and preserve missing tags as an explicit category."""
    if frame.empty:
        return pd.DataFrame(columns=[field, "cost_usd"])
    data = frame.copy()
    data[field] = data[field].replace("", "Untagged")
    return data.groupby(field, as_index=False).cost_usd.sum().sort_values("cost_usd", ascending=False)


def _resource_findings_for_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply the visible billing filters to the synthetic resource sidecar."""
    source = DEFAULT_OUTPUT.parent / "simulated_resource_findings.csv"
    if frame.empty or source.exists() is False or not frame.dataset_note.astype(str).str.contains("synthetic", case=False).all():
        return pd.DataFrame()
    findings = pd.read_csv(source, keep_default_na=False)
    findings["current_monthly_cost_usd"] = pd.to_numeric(findings.current_monthly_cost_usd)
    findings["estimated_savings_usd"] = pd.to_numeric(findings.estimated_savings_usd)
    findings["utilization_pct"] = pd.to_numeric(findings.utilization_pct, errors="coerce")
    for field in ("billing_month", "account_id", "region", "service", "team", "environment"):
        if field in findings and field in frame:
            allowed = set(frame[field].replace("", "Untagged").astype(str).unique())
            findings[field] = findings[field].replace("", "Untagged").astype(str)
            findings = findings[findings[field].isin(allowed)]
    return findings.reset_index(drop=True)


def simulated_optimization_findings(frame: pd.DataFrame) -> pd.DataFrame:
    """Load month-scoped simulated findings matching selected dimensions."""
    columns = ["billing_month", "resource_id", "account_id", "region", "service", "team", "environment", "category", "finding", "current_monthly_cost_usd", "estimated_savings_usd", "utilization_pct", "evidence", "recommended_action", "confidence", "status"]
    findings = _resource_findings_for_frame(frame)
    if findings.empty:
        return pd.DataFrame(columns=columns)
    if not findings.empty and "billing_month" in findings:
        findings = findings[findings.billing_month == frame.billing_month.max()]
    return findings[columns].sort_values("estimated_savings_usd", ascending=False).reset_index(drop=True)


def monthly_savings_estimates(frame: pd.DataFrame) -> pd.DataFrame:
    """Return monthly sums of simulated estimated savings for selected accounts."""
    columns = ["billing_month", "estimated_savings_usd"]
    findings = _resource_findings_for_frame(frame)
    if findings.empty or not {"billing_month", "estimated_savings_usd"}.issubset(findings.columns):
        return pd.DataFrame(columns=columns)
    return findings.groupby("billing_month", as_index=False).estimated_savings_usd.sum().sort_values("billing_month")


def budget_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Compare illustrative monthly budgets with the latest represented month."""
    budget_path = DEFAULT_OUTPUT.parent / "illustrative_team_budgets.csv"
    if frame.empty or not frame.dataset_note.astype(str).str.contains("synthetic", case=False).all():
        return pd.DataFrame(columns=["team", "budget_usd", "actual_usd", "utilization_pct", "forecast_usd", "overrun_usd"])
    latest_month = frame.billing_date.max().to_period("M")
    month_frame = frame[frame.billing_date.dt.to_period("M") == latest_month]
    actuals = allocation_summary(month_frame, "team").rename(columns={"team": "team"})
    if actuals.empty:
        return pd.DataFrame(columns=["team", "budget_usd", "actual_usd", "utilization_pct", "forecast_usd", "overrun_usd"])
    actuals["team"] = actuals.team.replace("", "Untagged")
    if not budget_path.exists():
        return pd.DataFrame(columns=["team", "budget_usd", "actual_usd", "utilization_pct", "forecast_usd", "overrun_usd"])
    budgets = pd.read_csv(budget_path)
    budgets = budgets[budgets.billing_month == str(latest_month)]
    actuals = actuals.merge(budgets[["team", "budget_usd"]], on="team", how="left")
    actuals["budget_usd"] = pd.to_numeric(actuals.budget_usd, errors="coerce")
    days_elapsed = max(1, int(month_frame.billing_date.dt.day.max()))
    days_in_month = int(latest_month.days_in_month)
    actuals = actuals.rename(columns={"cost_usd": "actual_usd"})
    actuals["utilization_pct"] = actuals.actual_usd / actuals.budget_usd * 100
    actuals["forecast_usd"] = actuals.actual_usd / days_elapsed * days_in_month
    actuals["overrun_usd"] = (actuals.forecast_usd - actuals.budget_usd).clip(lower=0)
    return actuals[["team", "budget_usd", "actual_usd", "utilization_pct", "forecast_usd", "overrun_usd"]].sort_values("actual_usd", ascending=False)


def resource_findings_frame() -> pd.DataFrame:
    """Read resource findings sidecar without using billing-row approximations."""
    source = DEFAULT_OUTPUT.parent / "simulated_resource_findings.csv"
    if not source.exists():
        return pd.DataFrame()
    return pd.read_csv(source, keep_default_na=False)


def commitment_coverage_frame() -> pd.DataFrame:
    """Read synthetic Savings Plan and RI coverage/utilization metrics."""
    source = DEFAULT_OUTPUT.parent / "simulated_commitment_coverage.csv"
    if not source.exists():
        return pd.DataFrame()
    return pd.read_csv(source, keep_default_na=False)


def build_insights(frame: pd.DataFrame, anomalies: pd.DataFrame | None = None,
                   findings: pd.DataFrame | None = None, budgets: pd.DataFrame | None = None) -> list[dict[str, str]]:
    """Build ranked, data-derived insight cards with impact, driver, and action."""
    if frame.empty or not frame.dataset_note.astype(str).str.contains("synthetic", case=False).all():
        return []
    cards: list[dict[str, str]] = []
    months = sorted(frame.billing_month.unique())
    action_by_service = {
        "NAT Gateway": "Review traffic paths and evaluate VPC endpoints for high-volume AWS service traffic.",
        "Data Transfer": "Trace the largest transfer usage type and validate cross-region data movement.",
        "Amazon EC2": "Review instance sizing, schedules, and recent capacity changes.",
        "Amazon RDS": "Review instance class, utilization, and non-production schedules.",
        "Amazon CloudWatch": "Inspect log groups and retention settings for unexpected ingestion growth.",
        "Amazon S3": "Review access patterns and lifecycle policies for infrequently accessed data.",
    }
    if len(months) > 1:
        previous, current = months[-2:]
        totals = frame[frame.billing_month.isin([previous, current])].groupby(["service", "billing_month"]).cost_usd.sum().unstack(fill_value=0)
        totals = totals.reindex(columns=[previous, current], fill_value=0)
        diff = totals[current] - totals[previous]
        service = diff.abs().idxmax()
        delta = float(diff[service])
        current_rows = frame[(frame.billing_month == current) & (frame.service == service)]
        usage = current_rows.groupby("usage_type").cost_usd.sum().idxmax() if not current_rows.empty else service
        cards.append({"icon":"monitoring", "headline":f"{service} spend changed {_money_placeholder(delta)} from {previous} to {current}.",
                      "impact":format_usd(delta), "driver":f"Largest current usage category: {usage}.",
                      "action":action_by_service.get(service,"Review the largest usage category and compare its workload change."), "kind":"mover"})
    total = float(frame.cost_usd.sum())
    missing = frame[(frame.team == "") | (frame.project == "") | (frame.environment == "")]
    untagged = float(missing.cost_usd.sum())
    if total > 0:
        leading = missing.groupby("service").cost_usd.sum().sort_values(ascending=False) if not missing.empty else pd.Series(dtype=float)
        driver = leading.index[0] if len(leading) else "none"
        cards.append({"icon":"sell", "headline":f"{untagged / total:.1%} of spend has incomplete allocation tags.",
                      "impact":format_usd(untagged), "driver":f"Largest incomplete-tag service: {driver}.",
                      "action":"Add or repair team, project, and environment tags at the source; keep unknown spend unassigned.", "kind":"allocation"})
    if findings is not None and not findings.empty:
        row = findings.sort_values("estimated_savings_usd", ascending=False).iloc[0]
        cards.append({"icon":"savings", "headline":f"{row.finding} is the largest simulated opportunity.",
                      "impact":f"{format_usd(float(row.estimated_savings_usd))} estimated per month", "driver":str(row.evidence),
                      "action":str(row.recommended_action), "kind":"savings"})
    if budgets is not None and not budgets.empty and budgets.overrun_usd.notna().any():
        row = budgets.sort_values("overrun_usd", ascending=False).iloc[0]
        if row.overrun_usd > 0:
            cards.append({"icon":"account_balance_wallet", "headline":f"{row.team} is projected above its illustrative monthly budget.",
                          "impact":f"{format_usd(float(row.overrun_usd))} projected overrun", "driver":f"Projected utilization {float(row.forecast_usd)/float(row.budget_usd):.1%}.",
                          "action":"Review the largest current-month services and confirm the planning budget assumptions.", "kind":"budget"})
    if anomalies is not None and not anomalies.empty:
        row = anomalies.sort_values("change_usd", ascending=False).iloc[0]
        event = str(row.scenario or "Observed cost above weekday baseline")
        cards.append({"icon":"warning", "headline":f"{row.service} has the largest weekday-baseline anomaly.",
                      "impact":f"{format_usd(float(row.change_usd))} above baseline", "driver":f"{row.probable_driver} · {event} · {row.account_id}/{row.region}.",
                      "action":action_by_service.get(row.service,"Inspect the dominant usage category and confirm the workload change."), "kind":"anomaly"})
    return cards[:5]


def _money_placeholder(amount: float) -> str:
    return format_usd(abs(amount))
