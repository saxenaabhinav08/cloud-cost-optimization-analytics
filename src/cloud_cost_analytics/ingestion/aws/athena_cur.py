"""Safe, partition-bounded Athena reader for legacy CUR and CUR 2.0."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
import time
from typing import Callable

from .config import AwsConfig, IDENTIFIER_RE, create_boto3_client
from .errors import (AwsAccessDeniedError, AwsRequestError, ConfigError, PollTimeoutError,
                     RequestBudgetExceededError, ScanLimitExceededError)
from .redact import redact_text

_COST_COLUMNS = {
    "UnblendedCost": "line_item_unblended_cost",
    "AmortizedCost": "reservation_effective_cost",
    "NetUnblendedCost": "line_item_net_unblended_cost",
    "NetAmortizedCost": "reservation_net_effective_cost",
}
_TAG_KEY_RE = re.compile(r"^[A-Za-z0-9_.:/ -]{1,128}$")

# Explicit source mappings keep format-specific CUR strings in one place.
CUR_COLUMN_MAPS: dict[str, dict[str, str]] = {
    "legacy_cur": {
        "account": "line_item_usage_account_id", "usage_date": "line_item_usage_start_date",
        "service": "product_product_name", "region": "product_region",
        "usage_type": "line_item_usage_type", "line_item_type": "line_item_line_item_type",
        "unblended_cost": "line_item_unblended_cost", "amortized_cost": "reservation_effective_cost",
        "net_unblended_cost": "line_item_net_unblended_cost", "net_amortized_cost": "reservation_net_effective_cost",
        "currency": "line_item_currency_code", "tags": "resource_tags", "bill_period": "bill_billing_period_start_date",
    },
    "cur2": {
        "account": "line_item_usage_account_id", "usage_date": "line_item_usage_start_date",
        "service": "product_product_name", "region": "product_region_code",
        "usage_type": "line_item_usage_type", "line_item_type": "line_item_line_item_type",
        "unblended_cost": "line_item_unblended_cost", "amortized_cost": "reservation_effective_cost",
        "net_unblended_cost": "line_item_net_unblended_cost", "net_amortized_cost": "reservation_net_effective_cost",
        "currency": "line_item_currency_code", "tags": "resource_tags", "bill_period": "bill_billing_period_start_date",
    },
}


@dataclass(frozen=True)
class AthenaQueryPlan:
    """Reviewable SQL and strongly typed bind values, built without AWS calls."""
    sql: str
    parameters: tuple[object, ...]
    cur_format: str
    partition_year: int
    partition_month: int

    def execution_parameters(self) -> list[str]:
        """Encode validated values in Athena's ExecutionParameters string format."""
        result = []
        for value in self.parameters:
            if isinstance(value, date):
                result.append("'" + value.isoformat() + "'")
            elif isinstance(value, int):
                result.append("'" + str(value) + "'")
            else:
                # Free-form values are not accepted here; tag keys are validated
                # separately and serialized as a bound SQL string value.
                raw = str(value)
                if "'" in raw or "\\" in raw or any(ord(char) < 32 for char in raw):
                    raise ValueError("Unsafe Athena parameter")
                result.append("'" + raw + "'")
        return result


def plan_athena_query(*, database: str, table: str, cur_format: str, cost_metric: str,
                      year: int, month: int, start_date: date, end_date: date,
                      tag_key: str | None = None) -> AthenaQueryPlan:
    """Build one-month CUR SQL with required partitions and bound values only."""
    for name, value in (("AWS_ATHENA_DATABASE", database), ("AWS_ATHENA_TABLE", table)):
        if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
            raise ConfigError(f"Invalid identifier in {name}.")
    if cur_format not in CUR_COLUMN_MAPS:
        raise ConfigError("AWS_CUR_FORMAT must be legacy_cur or cur2.")
    if cost_metric not in _COST_COLUMNS:
        raise ConfigError("AWS_COST_METRIC is unsupported for CUR.")
    if type(year) is not int or not 2000 <= year <= 9999 or type(month) is not int or not 1 <= month <= 12:
        raise ValueError("year and month must identify a valid billing partition")
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        raise ValueError("start_date and end_date must be datetime.date objects")
    if start_date >= end_date or (start_date.year, start_date.month) != (year, month):
        raise ValueError("query dates must be a non-empty range within the selected partition")
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    if end_date > date(year, month, last_day) + timedelta(days=1):
        raise ValueError("end_date is outside the selected billing partition")
    if tag_key is not None and (not isinstance(tag_key, str) or not _TAG_KEY_RE.fullmatch(tag_key)):
        raise ValueError("tag key contains unsupported characters")

    cols = CUR_COLUMN_MAPS[cur_format]
    fields = ["account", "usage_date", "service", "region", "usage_type", "line_item_type", "currency", "tags", "bill_period"]
    cost_column = _COST_COLUMNS[cost_metric]
    expressions = [f'"{cols[key]}" AS "{alias}"' for key, alias in zip(fields, (
        "aws_account_id", "usage_date", "service", "region", "usage_type", "line_item_type",
        "currency", "tags", "bill_period",
    ))]
    expressions.append(f'"{cost_column}" AS "cost"')
    table_ref = f'"{database}"."{table}"'
    conditions = ['"year" = ?', '"month" = ?', f'"{cols["usage_date"]}" >= CAST(? AS TIMESTAMP)',
                  f'"{cols["usage_date"]}" < CAST(? AS TIMESTAMP)']
    params: tuple[object, ...] = (year, f"{month:02d}", start_date, end_date)
    # Athena maps CUR 2 resource_tags as map<varchar,varchar>. The key remains a
    # bound execution value; it is never interpolated into SQL text.
    if tag_key is not None:
        conditions.append(f'element_at("{cols["tags"]}", ?) IS NOT NULL')
        params += (tag_key,)
    sql = "SELECT " + ", ".join(expressions) + f" FROM {table_ref} WHERE " + " AND ".join(conditions)
    if not re.search(r'"year"\s*=\s*\?', sql, re.IGNORECASE) or not re.search(r'"month"\s*=\s*\?', sql, re.IGNORECASE):
        raise ValueError("Athena query must include both year and month partition predicates")
    return AthenaQueryPlan(sql=sql, parameters=params, cur_format=cur_format,
                           partition_year=year, partition_month=month)


class AthenaCurClient:
    """Run reviewable Athena CUR plans with bounded scan and polling policies."""

    def __init__(self, config: AwsConfig, client=None, *, sleep_fn: Callable[[float], None] = time.sleep,
                 clock_fn: Callable[[], float] = time.monotonic):
        config.require_athena()
        self.config = config
        self.client = client if client is not None else create_boto3_client("athena", config)
        self.sleep = sleep_fn
        self.clock = clock_fn
        self.request_count = 0
        self.last_bytes_scanned = 0

    def _consume_request(self) -> None:
        """Reserve one bounded Athena API call before sending it."""
        if self.request_count >= self.config.max_api_requests:
            raise RequestBudgetExceededError(
                f"Athena request budget ({self.config.max_api_requests}) exceeded; no further request was sent."
            )
        self.request_count += 1

    def plan(self, *, year: int, month: int, start_date: date, end_date: date,
             tag_key: str | None = None) -> AthenaQueryPlan:
        self.config.require_athena()
        return plan_athena_query(database=self.config.athena_database or "", table=self.config.athena_table or "",
            cur_format=self.config.cur_format, cost_metric=self.config.cost_metric,
            year=year, month=month, start_date=start_date, end_date=end_date, tag_key=tag_key)

    def query(self, plan: AthenaQueryPlan, *, dry_run: bool = False) -> list[dict[str, object]] | AthenaQueryPlan:
        """Execute a plan unless dry_run is requested; paginate rows and enforce scan cap."""
        if dry_run:
            return plan
        self.config.require_athena()
        self.request_count = 0
        self.last_bytes_scanned = 0
        self._consume_request()
        try:
            started = self.client.start_query_execution(
                QueryString=plan.sql,
                ExecutionParameters=plan.execution_parameters(),
                QueryExecutionContext={"Database": self.config.athena_database},
                ResultConfiguration={"OutputLocation": self.config.athena_output_location},
                WorkGroup=self.config.athena_workgroup,
            )
        except Exception as exc:
            self._raise_client_error(exc, "Athena could not start the CUR query.")
        query_id = started.get("QueryExecutionId")
        if not query_id:
            raise AwsRequestError("Athena did not return a query execution ID.")
        deadline = self.clock() + self.config.poll_timeout_seconds
        execution = None
        while self.clock() < deadline:
            self._consume_request()
            try:
                execution = self.client.get_query_execution(QueryExecutionId=query_id).get("QueryExecution", {})
            except Exception as exc:
                self._raise_client_error(exc, "Athena query status could not be read.")
            status = execution.get("Status", {}).get("State")
            if status == "SUCCEEDED":
                break
            if status in {"FAILED", "CANCELLED"}:
                reason = redact_text(execution.get("Status", {}).get("StateChangeReason", "no reason supplied"))
                raise AwsRequestError(f"Athena query {status.lower()}: {reason}")
            if status not in {"QUEUED", "RUNNING"}:
                raise AwsRequestError("Athena returned an unknown query state.")
            self.sleep(min(self.config.poll_interval_seconds, max(0.0, deadline - self.clock())))
        else:
            try:
                self._consume_request()
                self.client.stop_query_execution(QueryExecutionId=query_id)
            except Exception:
                pass
            raise PollTimeoutError("Athena query exceeded the configured polling timeout and was asked to stop.")

        scanned = int(execution.get("Statistics", {}).get("DataScannedInBytes", 0) or 0)
        self.last_bytes_scanned = scanned
        if scanned > self.config.max_bytes_scanned:
            raise ScanLimitExceededError(
                f"Athena scanned {scanned} bytes, above the configured {self.config.max_bytes_scanned}-byte client limit."
            )
        return self._results(query_id)

    def _results(self, query_id: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        columns: list[str] | None = None
        token = None
        pages = 0
        while True:
            if pages >= self.config.max_pages:
                raise AwsRequestError(f"Athena exceeded the configured results page cap ({self.config.max_pages}).")
            args = {"QueryExecutionId": query_id}
            if token:
                args["NextToken"] = token
            self._consume_request()
            try:
                response = self.client.get_query_results(**args)
            except Exception as exc:
                self._raise_client_error(exc, "Athena query results could not be read.")
            pages += 1
            result_set = response.get("ResultSet", {})
            if columns is None:
                columns = [item.get("Name", "") for item in result_set.get("ResultSetMetadata", {}).get("ColumnInfo", [])]
            for row_index, item in enumerate(result_set.get("Rows", [])):
                values = [cell.get("VarCharValue") for cell in item.get("Data", [])]
                # Athena returns column names as the first row on the first page.
                if pages == 1 and row_index == 0 and values == columns:
                    continue
                padded = values + [None] * max(0, len(columns or []) - len(values))
                rows.append(dict(zip(columns or [], padded)))
            token = response.get("NextToken")
            if not token:
                return rows

    @staticmethod
    def _raise_client_error(exc: Exception, fallback: str) -> None:
        response = getattr(exc, "response", {}) or {}
        code = str(response.get("Error", {}).get("Code", ""))
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            raise AwsAccessDeniedError("Athena access denied. Check the scoped query, Glue read and result-prefix permissions.") from None
        message = redact_text(code or type(exc).__name__)
        raise AwsRequestError(f"{fallback} ({message}).") from None
