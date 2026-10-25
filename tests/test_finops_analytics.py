from datetime import date

from cloud_cost_analytics.finops_analytics import (
    allocation_summary, anomaly_events, apply_filters, budget_summary, build_insights,
    detection_quality, forecast_month_end, load_ground_truth, monthly_savings_estimates,
    simulated_optimization_findings, synthetic_frame,
)
from cloud_cost_analytics.synthetic_data import generate_synthetic_billing, validate_synthetic_billing


def test_generator_is_deterministic_and_contains_documented_variety():
    first = generate_synthetic_billing(start=date(2025, 1, 1), end=date(2025, 1, 3), seed=7)
    second = generate_synthetic_billing(start=date(2025, 1, 1), end=date(2025, 1, 3), seed=7)
    assert first == second
    assert validate_synthetic_billing(first) == 3 * 3 * 15
    assert all("SYNTHETIC" in row["dataset_note"] for row in first)


def test_generated_file_has_daily_rows_and_allocation_gaps():
    frame = synthetic_frame()
    assert len(frame) == 16_425
    assert frame.account_id.nunique() == 3
    assert frame.service.nunique() == 12
    assert frame.region.nunique() == 4
    assert frame.billing_date.min() == frame.billing_date.max().replace(month=1, day=1)
    assert (frame[["team", "project", "environment"]] == "").any(axis=None)
    assert frame.dataset_note.str.contains("SYNTHETIC").all()


def test_filters_and_allocation_preserve_untagged_values():
    frame = synthetic_frame()
    filtered = apply_filters(frame, {"start": date(2025, 2, 1), "end": date(2025, 2, 3), "service": ["Amazon EC2"]})
    assert len(filtered) == 18
    filtered.loc[filtered.index[0], "team"] = ""
    team = allocation_summary(filtered, "team")
    assert "Untagged" in team.team.tolist()


def test_month_end_forecast_and_idle_resource_estimates():
    frame = synthetic_frame()
    sample = frame[frame.billing_date <= "2025-01-05"]
    result = forecast_month_end(sample)
    assert result["month"] == "2025-01"
    assert result["days_elapsed"] == 5
    findings = simulated_optimization_findings(sample)
    assert not findings.empty
    assert findings.billing_month.eq("2025-01").all()
    assert (findings.estimated_savings_usd > 0).all()


def test_monthly_savings_history_is_month_scoped_and_supports_real_comparison():
    frame = synthetic_frame()
    history = monthly_savings_estimates(frame)
    latest = simulated_optimization_findings(frame)
    assert len(history) == 12
    assert history.billing_month.is_monotonic_increasing
    assert round(history.iloc[-1].estimated_savings_usd, 2) == round(latest.estimated_savings_usd.sum(), 2)
    assert len(latest) == 21


def test_resource_opportunities_and_savings_history_follow_billing_filters():
    frame = synthetic_frame()
    filtered = apply_filters(frame, {
        "start": date(2025, 12, 1), "end": date(2025, 12, 31),
        "service": ["Amazon EC2"], "account_id": ["synthetic-001"],
    })
    findings = simulated_optimization_findings(filtered)
    history = monthly_savings_estimates(filtered)
    assert findings.billing_month.eq("2025-12").all()
    assert findings.account_id.eq("synthetic-001").all()
    assert findings.service.eq("Amazon EC2").all()
    assert len(history) == 1 and history.billing_month.iloc[0] == "2025-12"


def test_synthetic_detection_quality_and_insights_are_explainable():
    frame = synthetic_frame()
    detected = anomaly_events(frame)
    truth = load_ground_truth()
    quality = detection_quality(detected, truth)
    assert quality["true_positives"] > 0
    assert 0 <= quality["precision"] <= 1
    assert 0 <= quality["recall"] <= 1
    findings = simulated_optimization_findings(frame)
    insights = build_insights(frame, detected, findings, budget_summary(frame))
    assert insights
    assert all(item["impact"].startswith(("$", "+$", "-$")) for item in insights)
    assert all(item["action"].strip() for item in insights)
    assert "2025-12" in insights[0]["headline"]
    assert "2025-11" in insights[0]["headline"]
    assert "2025-11" not in insights[0]["driver"]


def test_budgets_compare_with_latest_month_not_entire_selected_year():
    frame = synthetic_frame()
    result = budget_summary(frame)
    december = frame[frame.billing_month == "2025-12"].cost_usd.sum()
    assert round(result.actual_usd.sum(), 2) == round(december, 2)
    assert result.budget_usd.sum() > 0


def test_anomaly_rule_detects_known_injected_nat_scenario():
    frame = synthetic_frame()
    flags = anomaly_events(frame)
    nat = flags[(flags.service == "NAT Gateway") & (flags.scenario.str.contains("runaway_nat_gateway", na=False))]
    assert not nat.empty
    assert (nat.change_pct > 150).all()
