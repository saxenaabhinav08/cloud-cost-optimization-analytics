"""Offline, read-only question answering over the selected dashboard data.

The built-in answers use fixed SQL templates against an in-memory SQLite copy of
the filtered rows. No query is sent to AWS or to an external model.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
import json
import os
from typing import Any
from urllib.request import Request, urlopen

import pandas as pd
import sqlglot
from sqlglot import exp

from .charts import format_usd
from .finops_analytics import anomaly_events, budget_summary, simulated_optimization_findings

MAX_ROWS = 500
ALLOWED_COLUMNS = {
    "billing_date", "billing_month", "service", "cost_usd", "region", "environment",
    "team", "project", "account_id", "usage_type", "resource_id", "resource_state",
    "observed_usd", "baseline_usd", "change_usd", "change_pct", "severity",
    "probable_driver", "finding", "current_monthly_cost_usd", "estimated_savings_usd",
    "evidence", "recommended_action", "budget_usd", "actual_usd", "utilization_pct",
    "forecast_usd", "overrun_usd", "week", "spend", "previous_spend", "current_spend",
    "change_usd", "change_pct", "row_count", "team_name", "month", "day",
}
ALLOWED_TABLES = {"billing_records", "anomalies", "resource_findings", "team_budgets"}
ALLOWED_FUNCTIONS = {
    "Sum", "Count", "Coalesce", "StrToTime", "Lag", "Round", "Max", "Min", "Avg",
    "Abs", "Date", "Cast", "If", "Nullif", "Lower", "TimeToStr", "TsOrDsToTimestamp",
    "Or", "And",
}


class UnsafeQuery(ValueError):
    """Raised when a query exceeds the in-memory read-only policy."""


def validate_readonly_sql(sql: str, *, max_rows: int = MAX_ROWS) -> str:
    """Validate one SELECT against an explicit table/column allowlist and cap rows."""
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeQuery("A non-empty SELECT query is required.")
    if re.search(r"--|/\*|\*/|(^|\s)#", sql):
        raise UnsafeQuery("SQL comments are not allowed.")
    try:
        parsed = [node for node in sqlglot.parse(sql, read="sqlite") if node is not None]
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQuery("The query could not be parsed safely.") from exc
    if len(parsed) != 1:
        raise UnsafeQuery("Only one SQL statement is allowed.")
    tree = parsed[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise UnsafeQuery("Only SELECT queries are allowed.")
    cte_names = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
    tables = {table.name.casefold() for table in tree.find_all(exp.Table)} - cte_names
    if not tables or not tables.issubset(ALLOWED_TABLES):
        raise UnsafeQuery("The query references a table outside the allowlist.")
    aliases = {projection.alias.casefold() for projection in tree.find_all(exp.Alias) if projection.alias}
    for column in tree.find_all(exp.Column):
        if column.is_star:
            continue
        name = column.name.casefold()
        if name not in ALLOWED_COLUMNS and name not in aliases:
            raise UnsafeQuery(f"The query references a column outside the allowlist: {name}.")
        if column.table and column.table.casefold() not in {table.alias_or_name.casefold() for table in tree.find_all(exp.Table)}:
            raise UnsafeQuery("The query references an unknown table alias.")
    for function in tree.find_all(exp.Func):
        if type(function).__name__ not in ALLOWED_FUNCTIONS:
            raise UnsafeQuery("The query contains a function outside the allowlist.")
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(max_rows)
    else:
        value = limit.expression
        if isinstance(value, exp.Literal) and not value.is_string:
            try:
                if int(value.this) > max_rows:
                    tree = tree.limit(max_rows)
            except (TypeError, ValueError):
                raise UnsafeQuery("LIMIT must be a numeric value.")
        elif not isinstance(value, (exp.Placeholder, exp.Parameter)):
            raise UnsafeQuery("LIMIT must be a numeric value or a bound parameter.")
    return tree.sql(dialect="sqlite")


@dataclass(frozen=True)
class QuerySpec:
    intent: str
    sql: str
    params: dict[str, Any]
    title: str


_QUERIES = {
    "top_service": QuerySpec("top_service", "SELECT service, SUM(cost_usd) AS spend, COUNT(*) AS row_count FROM billing_records GROUP BY service ORDER BY spend DESC LIMIT :limit", {"limit": 20}, "Top services"),
    "spend_change": QuerySpec("spend_change", "WITH monthly AS (SELECT billing_month, SUM(cost_usd) AS spend FROM billing_records GROUP BY billing_month), compared AS (SELECT billing_month, spend, LAG(spend) OVER (ORDER BY billing_month) AS previous_spend FROM monthly) SELECT billing_month, previous_spend, spend AS current_spend, spend - previous_spend AS change_usd, (spend - previous_spend) * 100.0 / NULLIF(previous_spend, 0) AS change_pct FROM compared WHERE previous_spend IS NOT NULL ORDER BY billing_month DESC LIMIT 2", {}, "Monthly spend change"),
    "movers": QuerySpec("movers", "WITH monthly AS (SELECT service, billing_month, SUM(cost_usd) AS spend FROM billing_records GROUP BY service, billing_month), ranked AS (SELECT service, billing_month, spend, LAG(spend) OVER (PARTITION BY service ORDER BY billing_month) AS previous_spend FROM monthly) SELECT service, previous_spend, spend AS current_spend, spend - previous_spend AS change_usd, (spend - previous_spend) * 100.0 / NULLIF(previous_spend, 0) AS change_pct FROM ranked WHERE previous_spend IS NOT NULL ORDER BY billing_month DESC, ABS(spend-previous_spend) DESC LIMIT 30", {}, "Largest service movers"),
    "untagged": QuerySpec("untagged", "SELECT COALESCE(NULLIF(team, ''), 'Untagged') AS team_name, service, SUM(cost_usd) AS spend FROM billing_records WHERE COALESCE(team, '') = '' OR COALESCE(project, '') = '' OR COALESCE(environment, '') = '' GROUP BY team_name, service ORDER BY spend DESC LIMIT :limit", {"limit": 30}, "Untagged spend by team"),
    "anomaly": QuerySpec("anomaly", "SELECT billing_date, service, account_id, region, observed_usd, baseline_usd, change_usd, change_pct, severity, probable_driver FROM anomalies ORDER BY change_usd DESC LIMIT :limit", {"limit": 20}, "Largest detected anomalies"),
    "savings": QuerySpec("savings", "SELECT finding, service, team, resource_id, estimated_savings_usd, evidence, recommended_action FROM resource_findings ORDER BY estimated_savings_usd DESC LIMIT :limit", {"limit": 30}, "Simulated savings opportunities"),
    "budget": QuerySpec("budget", "SELECT team AS team_name, budget_usd, actual_usd, utilization_pct, forecast_usd, overrun_usd FROM team_budgets ORDER BY overrun_usd DESC LIMIT :limit", {"limit": 30}, "Illustrative budget risk by team"),
    "region": QuerySpec("region", "SELECT region, SUM(cost_usd) AS spend FROM billing_records GROUP BY region ORDER BY spend DESC LIMIT :limit", {"limit": 30}, "Spend by region"),
    "environment": QuerySpec("environment", "SELECT COALESCE(NULLIF(environment, ''), 'Untagged') AS environment, SUM(cost_usd) AS spend FROM billing_records GROUP BY environment ORDER BY spend DESC LIMIT :limit", {"limit": 30}, "Spend by environment"),
    "forecast": QuerySpec("forecast", "SELECT billing_date, SUM(cost_usd) AS spend FROM billing_records GROUP BY billing_date ORDER BY billing_date DESC LIMIT 31", {}, "Month-end run-rate estimate"),
    "idle": QuerySpec("idle", "SELECT finding, service, team, resource_id, current_monthly_cost_usd, estimated_savings_usd, evidence, recommended_action FROM resource_findings WHERE LOWER(finding) LIKE '%idle%' OR LOWER(finding) LIKE '%unattached%' OR LOWER(finding) LIKE '%unused%' ORDER BY estimated_savings_usd DESC LIMIT :limit", {"limit": 30}, "Simulated idle-resource findings"),
    "weekly": QuerySpec("weekly", "WITH weekly AS (SELECT STRFTIME('%Y-%W', billing_date) AS week, SUM(cost_usd) AS spend FROM billing_records GROUP BY week), compared AS (SELECT week, spend, LAG(spend) OVER (ORDER BY week) AS previous_spend FROM weekly) SELECT week, previous_spend, spend AS current_spend, spend-previous_spend AS change_usd, (spend-previous_spend)*100.0/NULLIF(previous_spend,0) AS change_pct FROM compared WHERE previous_spend IS NOT NULL ORDER BY week DESC LIMIT 2", {}, "Week-over-week change"),
    "team_breakdown": QuerySpec("team_breakdown", "SELECT COALESCE(NULLIF(team, ''), 'Untagged') AS team_name, SUM(cost_usd) AS spend FROM billing_records GROUP BY team_name ORDER BY spend DESC LIMIT :limit", {"limit": 30}, "Spend by team"),
}


def match_intent(question: str, previous_intent: str | None = None) -> str | None:
    """Map a natural-language question to one of twelve fixed query templates."""
    q = question.casefold()
    if any(term in q for term in ("break that down", "team breakdown")):
        return "team_breakdown"
    if any(term in q for term in ("week over week", "week-over-week", "weekly", "wow")):
        return "weekly"
    if any(term in q for term in ("idle", "unattached", "unused resource")):
        return "idle"
    if any(term in q for term in ("forecast", "month-end", "month end", "run rate", "run-rate")):
        return "forecast"
    if "environment" in q:
        return "environment"
    if "region" in q:
        return "region"
    if "budget" in q or "overrun" in q:
        return "budget"
    if any(term in q for term in ("saving", "optimization", "opportunity")):
        return "savings"
    if any(term in q for term in ("anomaly", "anomalies", "spike", "review first")):
        return "anomaly"
    if any(term in q for term in ("untagged", "unallocated", "missing tag")):
        return "untagged"
    if "by team" in q:
        return "team_breakdown"
    if any(term in q for term in ("why did", "mover", "what moved", "drivers")):
        return "movers"
    if any(term in q for term in ("change", "last month", "month over month", "mom", "increased", "decreased")):
        return "spend_change"
    if any(term in q for term in ("service", "most", "highest", "top")):
        return "top_service"
    if previous_intent and any(term in q for term in ("break that", "by team")):
        return "team_breakdown"
    return None


def _as_table(frame: pd.DataFrame, name: str, connection: sqlite3.Connection) -> None:
    if frame is None:
        frame = pd.DataFrame()
    safe = frame.copy()
    for column in safe:
        if pd.api.types.is_datetime64_any_dtype(safe[column]):
            safe[column] = safe[column].astype(str)
    safe.to_sql(name, connection, if_exists="replace", index=False)


def generate_sql_with_openai(question: str, api_key: str, *, timeout: int = 15) -> str:
    """Request SQL only from OpenAI when the user has explicitly enabled it.

    An API request may incur charges; callers must require an explicit opt-in.
    Only the question and this fixed schema description are sent, never billing rows.
    """
    schema = ("Allowed SQLite tables and column descriptions: billing_records has billing_date (charge date), billing_month (YYYY-MM), service (AWS service label), cost_usd (fictional or local row cost in USD), region, environment/team/project (allocation tags; blank means untagged), account_id, usage_type, resource_id, and resource_state. "
              "anomalies has billing_date, service, account_id, region, observed_usd, baseline_usd, change_usd, change_pct, severity, and probable_driver from the local rule detector. "
              "resource_findings has finding, service, team, resource_id, current_monthly_cost_usd, estimated_savings_usd (simulated only), evidence, and recommended_action. "
              "team_budgets has team, budget_usd (illustrative), actual_usd, utilization_pct, forecast_usd, and overrun_usd. Return one SQLite SELECT only, no comments, writes, or DDL.")
    body = {"model": os.getenv("ASK_AI_MODEL", "gpt-4o-mini"), "temperature": 0,
            "messages": [{"role": "system", "content": f"Convert the user's question to one SQL SELECT. {schema}"},
                         {"role": "user", "content": question}]}
    request = Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    sql = str(payload["choices"][0]["message"]["content"]).strip()
    fenced = re.fullmatch(r"```(?:sql)?\s*(.*?)\s*```", sql, flags=re.IGNORECASE | re.DOTALL)
    return fenced.group(1).strip() if fenced else sql


def run_question(question: str, frame: pd.DataFrame, *, previous_intent: str | None = None,
                 generated_sql: str | None = None) -> dict[str, Any]:
    """Run one matching canned query and return its answer, table, and executed SQL."""
    intent = "generated" if generated_sql else match_intent(question, previous_intent)
    if intent is None:
        return {"intent": None, "answer": "I couldn't match that question to a supported analysis. I can show the top service, explain monthly or weekly changes, or find untagged spend.", "sql": None, "rows": [], "executed": False}
    spec = _QUERIES.get(intent)
    if frame.empty:
        return {"intent": intent, "answer": "There are no selected billing records to analyze. Choose a data source and date range, then ask again.", "sql": generated_sql or (spec.sql if spec else None), "rows": [], "executed": False}
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        _as_table(frame, "billing_records", connection)
        sql = validate_readonly_sql(generated_sql or spec.sql)
        cte_names = {cte.alias_or_name.casefold() for cte in sqlglot.parse_one(sql, read="sqlite").find_all(exp.CTE)}
        needed = {table.name.casefold() for table in sqlglot.parse_one(sql, read="sqlite").find_all(exp.Table)} - cte_names
        if "anomalies" in needed:
            _as_table(anomaly_events(frame), "anomalies", connection)
        if "resource_findings" in needed:
            _as_table(simulated_optimization_findings(frame), "resource_findings", connection)
        if "team_budgets" in needed:
            _as_table(budget_summary(frame), "team_budgets", connection)
        connection.execute("PRAGMA query_only = ON")
        connection.set_progress_handler(lambda: 1, 500_000)
        raw = [dict(row) for row in connection.execute(sql, {} if generated_sql else spec.params).fetchall()]
    except (sqlite3.Error, UnsafeQuery) as exc:
        return {"intent": intent, "answer": f"The selected data could not answer this question ({type(exc).__name__}). No database writes or changes were made.", "sql": generated_sql or (spec.sql if spec else None), "rows": [], "executed": False}
    finally:
        connection.close()
    result = pd.DataFrame(raw)
    if intent == "forecast" and not result.empty:
        month = pd.to_datetime(frame.billing_date.max()).to_period("M")
        month_frame = frame[pd.to_datetime(frame.billing_date).dt.to_period("M") == month]
        actual = float(month_frame.cost_usd.sum())
        elapsed = max(1, int(pd.to_datetime(month_frame.billing_date).dt.day.max()))
        days = int(month.days_in_month)
        projected = actual / elapsed * days
        result = pd.DataFrame([{"billing_month": str(month), "month_to_date_usd": actual,
                                "elapsed_days": elapsed, "days_in_month": days,
                                "projected_month_end_usd": projected}])
    if result.empty:
        answer = ("The query returned no rows for the selected data. Try a broader billing date range." if spec is None
                  else f"No {spec.title.casefold()} rows are available for the selected data. Try a broader billing date range.")
    elif intent == "top_service":
        first = result.iloc[0]
        answer = f"{first.service} is the largest service in the selected billing rows at {format_usd(first.spend)}. The table ranks service totals; it does not explain the cause of the spend."
    elif intent == "spend_change":
        latest = result.iloc[0]
        answer = f"Spend in {latest.billing_month} was {format_usd(latest.current_spend)}, a change of {format_usd(latest.change_usd)} ({latest.change_pct:+.1f}%) from the preceding represented month. This is a calculated comparison, not a causal finding."
    elif intent in ("forecast",):
        row = result.iloc[0]
        answer = f"The selected month's straight-line run-rate projection is {format_usd(row.projected_month_end_usd)} from {format_usd(row.month_to_date_usd)} recorded through day {int(row.elapsed_days)}. This simple estimate assumes the month-to-date daily average continues."
    elif intent == "anomaly":
        row = result.iloc[0]
        answer = f"The largest rule-based flag is {row.service} on {str(row.billing_date)[:10]}, {format_usd(row.change_usd)} above its baseline. Its synthetic usage driver is {row.probable_driver or 'not specified'}; review the underlying record before acting."
    elif intent in ("budget",):
        row = result.iloc[0]
        answer = f"{row.team_name} has the largest illustrative projected budget overrun at {format_usd(row.overrun_usd)}. These planning budgets are fictional and are not AWS Budgets."
    elif intent in ("savings", "idle"):
        amount_column = "estimated_savings_usd"
        total = float(pd.to_numeric(result.get(amount_column, pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
        answer = f"The selected synthetic findings list {len(result)} opportunities with {format_usd(total)} in estimated monthly savings. These are simulated estimates, not realized savings; confirm ownership and evidence before taking action."
    elif intent == "untagged":
        spend = float(pd.to_numeric(result.spend, errors="coerce").fillna(0).sum())
        answer = f"Records with at least one missing allocation tag account for {format_usd(spend)} in this query result. “Untagged” means a team, project, or environment field is blank; it does not infer an owner."
    elif intent in ("weekly",):
        latest = result.iloc[0]
        answer = f"Week {latest.week} recorded {format_usd(latest.current_spend)}, changing by {format_usd(latest.change_usd)} ({latest.change_pct:+.1f}%) from the prior represented week. This comparison is descriptive, not causal."
    else:
        first_column = "change_usd" if "change_usd" in result else "spend" if "spend" in result else None
        amount = format_usd(float(result[first_column].iloc[0])) if first_column else f"{len(result)} rows"
        answer = f"The read-only query returned {len(result)} rows. The leading result is {amount}; review the result table for the full breakdown."
    return {"intent": intent, "answer": answer, "sql": sql, "params": {} if generated_sql else spec.params, "executed": True,
            "rows": result.to_dict(orient="records"), "columns": result.columns.tolist(),
            "title": spec.title if spec else "Generated read-only query"}
