"""Paginated, request-budgeted Cost Explorer reader (never called implicitly)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import random
import time
from typing import Callable, Iterable

from .config import AwsConfig, create_boto3_client
from .errors import (
    AwsAccessDeniedError, AwsIngestionDisabledError, AwsRequestError,
    AwsThrottledError, ConfigError, RequestBudgetExceededError,
)
from .redact import redact_text

_DIMENSIONS = {
    "LINKED_ACCOUNT", "SERVICE", "REGION", "USAGE_TYPE", "OPERATION",
    "AVAILABILITY_ZONE", "INSTANCE_TYPE", "PURCHASE_TYPE", "RECORD_TYPE",
    "OPERATING_SYSTEM", "TENANCY", "PLATFORM", "LEGAL_ENTITY_NAME",
    "DEPLOYMENT_OPTION", "DATABASE_ENGINE", "CACHE_ENGINE", "INSTANCE_TYPE_FAMILY",
}
_THROTTLE_CODES = {"Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException"}
_DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
_AUTH_CODES = {"InvalidClientTokenId", "UnrecognizedClientException", "ExpiredToken", "ExpiredTokenException"}


def _month_back(value: date, months: int) -> date:
    year, month = value.year, value.month - months
    while month <= 0:
        year -= 1
        month += 12
    day = min(value.day, (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)).day)
    return date(year, month, day)


def validate_date_range(start: date, end: date, *, today: date, max_lookback_months: int = 13) -> None:
    """Validate a half-open date range [start, end), with end excluded."""
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("start and end must be datetime.date values")
    if start >= end:
        raise ValueError("start must be earlier than the exclusive end date")
    if end > today:
        raise ValueError("exclusive end date cannot be in the future")
    if start < _month_back(today, max_lookback_months):
        raise ValueError(f"date range exceeds the configured {max_lookback_months}-month lookback")


def split_date_range(start: date, end: date, *, chunk_days: int = 30) -> list[tuple[date, date]]:
    """Split a half-open range into bounded requests without gaps or overlap."""
    if chunk_days < 1:
        raise ValueError("chunk_days must be positive")
    if start >= end:
        raise ValueError("start must be earlier than end")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        next_date = min(end, cursor + timedelta(days=chunk_days))
        chunks.append((cursor, next_date))
        cursor = next_date
    return chunks


def build_group_by(group_by: Iterable[str], *, tag_key: str | None = None) -> list[dict[str, str]]:
    """Build at most two Cost Explorer groups, always retaining linked account."""
    requested = [str(item).strip().upper() for item in group_by]
    if len(requested) != len(set(requested)):
        raise ValueError("GroupBy dimensions must be unique")
    if any(item not in _DIMENSIONS for item in requested):
        raise ValueError("Unsupported Cost Explorer GroupBy dimension")
    if "LINKED_ACCOUNT" not in requested:
        requested.insert(0, "LINKED_ACCOUNT")
    if tag_key is not None:
        key = str(tag_key).strip()
        if not key or any(ord(char) < 32 for char in key):
            raise ValueError("Invalid tag key")
        requested.append("TAG:" + key)
    if len(requested) > 2:
        raise ValueError("Cost Explorer accepts at most two GroupBy dimensions; linked account uses one")
    result = []
    for item in requested:
        if item.startswith("TAG:"):
            result.append({"Type": "TAG", "Key": item[4:]})
        else:
            result.append({"Type": "DIMENSION", "Key": item})
    return result


class CostExplorerClient:
    """Explicit Cost Explorer client with bounded dates, retries and API budget."""

    def __init__(self, config: AwsConfig, client=None, *, sleep_fn: Callable[[float], None] = time.sleep,
                 clock_fn: Callable[[], date] = date.today, jitter_fn: Callable[[float, float], float] = random.uniform):
        config.require_enabled()
        self.config = config
        self.client = client if client is not None else create_boto3_client("ce", config)
        self.sleep = sleep_fn
        self.today = clock_fn
        self.jitter = jitter_fn
        self.request_count = 0

    def _call(self, **kwargs):
        attempt = 0
        while True:
            if self.request_count >= self.config.max_api_requests:
                raise RequestBudgetExceededError(
                    f"Cost Explorer request budget ({self.config.max_api_requests}) exceeded; no further request was sent."
                )
            self.request_count += 1
            try:
                return self.client.get_cost_and_usage(**kwargs)
            except Exception as exc:
                response = getattr(exc, "response", {}) or {}
                code = str(response.get("Error", {}).get("Code", ""))
                status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                if code in _DENIED_CODES:
                    raise AwsAccessDeniedError("Cost Explorer access denied. Grant ce:GetCostAndUsage to the selected identity.") from None
                if code in _AUTH_CODES:
                    raise AwsRequestError("AWS authentication failed. Check the selected profile or role without sharing credentials.") from None
                throttled = code in _THROTTLE_CODES
                transient = throttled or int(status or 0) >= 500
                if transient and attempt + 1 < self.config.max_retry_attempts:
                    delay = min(8.0, 0.25 * (2 ** attempt)) + self.jitter(0, 0.25)
                    attempt += 1
                    self.sleep(delay)
                    continue
                if throttled:
                    raise AwsThrottledError("Cost Explorer remained throttled after the bounded retry policy.") from None
                safe_code = redact_text(code or type(exc).__name__)
                raise AwsRequestError(
                    f"Cost Explorer request failed ({safe_code}); verify the read-only permission and request parameters."
                ) from None

    def fetch(self, start: date, end: date, *, group_by: Iterable[str] = ("SERVICE",), tag_key: str | None = None,
              chunk_days: int = 30) -> list[dict[str, object]]:
        """Fetch daily rows for [start, end), following all pages and preserving estimates."""
        self.request_count = 0
        validate_date_range(start, end, today=self.today(), max_lookback_months=self.config.max_lookback_months)
        groups = build_group_by(group_by, tag_key=tag_key)
        request_hash = hashlib.sha256(json.dumps({"start": start.isoformat(), "end": end.isoformat(),
            "group_by": groups, "metric": self.config.cost_metric}, sort_keys=True).encode()).hexdigest()
        records: list[dict[str, object]] = []
        for chunk_start, chunk_end in split_date_range(start, end, chunk_days=chunk_days):
            token = None
            page_count = 0
            while True:
                if page_count >= self.config.max_pages:
                    raise AwsRequestError(f"Cost Explorer exceeded the configured page cap ({self.config.max_pages}).")
                args: dict[str, object] = {
                    "TimePeriod": {"Start": chunk_start.isoformat(), "End": chunk_end.isoformat()},
                    "Granularity": "DAILY", "Metrics": [self.config.cost_metric], "GroupBy": groups,
                }
                if token:
                    args["NextPageToken"] = token
                response = self._call(**args)
                page_count += 1
                for period in response.get("ResultsByTime", []):
                    period_start = date.fromisoformat(period["TimePeriod"]["Start"][:10])
                    estimated = bool(period.get("Estimated", False))
                    for group in period.get("Groups", []):
                        keys = list(group.get("Keys", []))
                        dimensions: dict[str, str] = {}
                        for descriptor, value in zip(groups, keys):
                            key = descriptor["Key"]
                            if descriptor["Type"] == "TAG":
                                dimensions["tag_value"] = str(value).split("$", 1)[-1]
                            else:
                                dimensions[key] = str(value)
                        metric = group.get("Metrics", {}).get(self.config.cost_metric, {})
                        try:
                            amount = Decimal(str(metric.get("Amount", "0")))
                        except InvalidOperation:
                            raise AwsRequestError("Cost Explorer returned a non-numeric amount.") from None
                        tags = {tag_key: dimensions.get("tag_value", "")} if tag_key else {}
                        records.append({
                            "aws_account_id": dimensions.get("LINKED_ACCOUNT", ""),
                            "usage_date": period_start,
                            "service": dimensions.get("SERVICE", "Aggregated service"),
                            "region": dimensions.get("REGION", "global"),
                            "usage_type": dimensions.get("USAGE_TYPE", "Aggregated"),
                            "line_item_type": "Aggregated", "cost": amount,
                            "currency": metric.get("Unit", "USD"), "cost_metric": self.config.cost_metric,
                            "is_estimated": estimated, "tags": tags, "source": "cost_explorer",
                            "source_version": request_hash,
                        })
                token = response.get("NextPageToken")
                if not token:
                    break
        return records


def create_cost_explorer_client(config: AwsConfig, **kwargs) -> CostExplorerClient:
    """Named factory that fails before importing boto3 when ingestion is disabled."""
    config.require_enabled()
    return CostExplorerClient(config, **kwargs)
