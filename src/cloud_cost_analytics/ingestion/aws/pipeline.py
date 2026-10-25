"""Opt-in orchestration for bounded Cost Explorer or CUR ingestion runs."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import uuid
from typing import Iterable, Mapping

from .athena_cur import AthenaCurClient
from .config import AwsConfig
from .cost_explorer import CostExplorerClient
from .errors import AwsIngestionDisabledError
from .normalize import NormalizationReport, normalize_records
from .redact import redact_text
from .repository import AwsRepository, WriteCounts
from .schema import CanonicalBillingRecord


@dataclass(frozen=True)
class IngestionSummary:
    run_id: str
    source: str
    scopes: tuple[str, ...]
    rows_read: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_rejected: int
    rows_deleted: int
    request_count: int
    bytes_scanned: int
    source_version: str
    malformed_sample_reasons: tuple[str, ...]


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def select_resync_scopes(today: date, *, resync_window_days: int = 90,
                         watermarks: Iterable[Mapping[str, object]] = (),
                         observed_versions: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Select current+recent months and older closed months whose source changed."""
    if resync_window_days < 1:
        raise ValueError("resync_window_days must be positive")
    months = max(1, math.ceil(resync_window_days / 31))
    current = _month_start(today)
    recent = [current]
    for _ in range(months - 1):
        recent.append(_previous_month(recent[-1]))
    selected = {item.strftime("%Y-%m") for item in recent}
    versions = observed_versions or {}
    for watermark in watermarks:
        scope = str(watermark.get("scope", ""))
        old_version = str(watermark.get("source_version", ""))
        if scope and scope not in selected and scope in versions and versions[scope] != old_version:
            selected.add(scope)
    return tuple(sorted(selected))


def authoritative_source_for_period(*, cur_available: bool, preference: str = "cur") -> str:
    """Choose one source per period so Cost Explorer and CUR are never summed."""
    if preference not in {"cur", "cost_explorer"}:
        raise ValueError("preference must be cur or cost_explorer")
    if preference == "cur" and cur_available:
        return "cur"
    if preference == "cost_explorer":
        return "cost_explorer"
    return "cost_explorer"


def authoritative_records(records: Iterable[CanonicalBillingRecord], *, preference: str = "cur",
                          cur_available_periods: Iterable[str] = ()) -> list[CanonicalBillingRecord]:
    """Keep only the configured authoritative source rows for each billing month."""
    rows = list(records)
    available = set(cur_available_periods)
    selected: dict[str, str] = {}
    for record in rows:
        period = record.usage_date.strftime("%Y-%m")
        if period not in selected:
            selected[period] = authoritative_source_for_period(
                cur_available=(period in available), preference=preference,
            )
    return [record for record in rows if record.source == selected[record.usage_date.strftime("%Y-%m")]]


class AwsIngestionPipeline:
    """Coordinate source reads, quarantine, atomic month replacements and watermarks.

    Constructing this orchestration object does not make a request. The run
    method checks the disabled-by-default flag before creating any AWS client.
    """

    def __init__(self, config: AwsConfig, repository: AwsRepository, *,
                 cost_explorer: CostExplorerClient | None = None,
                 athena: AthenaCurClient | None = None):
        self.config = config
        self.repository = repository
        self.cost_explorer = cost_explorer
        self.athena = athena
        self.authoritative_sources: dict[str, str] = {}

    def run_incremental(self, *, source: str, today: date | None = None,
                        observed_versions: Mapping[str, str] | None = None,
                        tag_key: str | None = None) -> tuple[IngestionSummary, ...]:
        """Re-sync the current/nearby periods and closed periods with new versions.

        Each period is a separate atomic ingestion run so its watermark advances
        only after that whole period succeeds. The default 90-day window selects
        the current month and the previous two months.
        """
        self.config.require_enabled()
        today = today or date.today()
        watermarks = self.repository.watermarks_for_source(source)
        scopes = select_resync_scopes(today, resync_window_days=self.config.resync_window_days,
                                      watermarks=watermarks, observed_versions=observed_versions)
        summaries: list[IngestionSummary] = []
        for scope in scopes:
            year, month = map(int, scope.split("-"))
            start = date(year, month, 1)
            end = min(date(year + (month == 12), month % 12 + 1, 1), today)
            if start >= end:
                continue
            summaries.append(self.run(source=source, start_date=start, end_date=end,
                                      tag_key=tag_key,
                                      source_versions={scope: (observed_versions or {}).get(scope, "")}))
        return tuple(summaries)

    def _get_cost_explorer(self) -> CostExplorerClient:
        if self.cost_explorer is None:
            self.cost_explorer = CostExplorerClient(self.config)
        return self.cost_explorer

    def _get_athena(self) -> AthenaCurClient:
        if self.athena is None:
            self.athena = AthenaCurClient(self.config)
        return self.athena

    def run(self, *, source: str, start_date: date, end_date: date,
            run_id: str | None = None, tag_key: str | None = None,
            source_versions: Mapping[str, str] | None = None,
            max_malformed_ratio: float | None = None) -> IngestionSummary:
        """Ingest half-open date range; all MySQL source scopes replace atomically."""
        if not self.config.enabled:
            self.config.require_enabled()
        if source not in {"cur", "cost_explorer"}:
            raise ValueError("source must be cur or cost_explorer")
        if not isinstance(start_date, date) or not isinstance(end_date, date) or start_date >= end_date:
            raise ValueError("start_date and end_date must be datetime.date values in a non-empty half-open range")
        run_key = run_id or str(uuid.uuid4())
        source_versions = source_versions or {}
        started_at = datetime.now(timezone.utc)
        self.repository.begin_run(run_key, source, started_at=started_at)
        raw_rows: list[Mapping[str, object]] = []
        reports: list[NormalizationReport] = []
        counts = WriteCounts()
        bytes_scanned = 0
        source_version = ""
        request_count = 0
        try:
            scopes = self._scopes_for_range(start_date, end_date)
            if source == "cost_explorer":
                client = self._get_cost_explorer()
                raw_rows = client.fetch(start_date, end_date,
                                        group_by=("SERVICE",) if tag_key is None else (), tag_key=tag_key)
                request_count = client.request_count
                source_version = str(raw_rows[0].get("source_version", "")) if raw_rows else ""
            else:
                client = self._get_athena()
                for scope in scopes:
                    year, month = map(int, scope.split("-"))
                    period_start = max(start_date, date(year, month, 1))
                    month_end = date(year + (month == 12), month % 12 + 1, 1)
                    period_end = min(end_date, month_end)
                    plan = client.plan(year=year, month=month, start_date=period_start, end_date=period_end, tag_key=tag_key)
                    batch = client.query(plan)
                    raw_rows.extend(batch)
                    request_count = int(getattr(client, "request_count", request_count))
                    bytes_scanned += int(getattr(client, "last_bytes_scanned", 0))
                    version = source_versions.get(scope, "")
                    if version:
                        source_version = version
                    for row in batch:
                        row.setdefault("source_version", version)
            report = normalize_records(
                raw_rows, source=source, cost_metric=self.config.cost_metric,
                source_version=source_version, ingestion_run_id=run_key,
                max_malformed_ratio=(self.config.max_malformed_ratio if max_malformed_ratio is None else max_malformed_ratio),
                tag_field_map=self.config.tag_field_map, start_date=start_date, end_date=end_date,
            )
            reports.append(report)
            records_by_scope: dict[str, list[CanonicalBillingRecord]] = defaultdict(list)
            for record in report.records:
                records_by_scope[record.usage_date.strftime("%Y-%m")].append(record)
            # Empty result months still replace their source scope (restatements
            # can legitimately remove every row in a period).
            for scope in scopes:
                scope_records = records_by_scope.get(scope, [])
                counts += self.repository.replace_scope(
                    source, scope, scope_records, run_id=run_key,
                    source_version=source_versions.get(scope, source_version),
                )
            self.repository.advance_watermarks(source, scopes, run_id=run_key, source_version=source_version)
            rejected = len(report.rejected)
            self.repository.finish_run(run_key, status="succeeded", rows_read=report.total_rows, counts=counts,
                rows_rejected=rejected, source_version=source_version, request_count=request_count,
                bytes_scanned=bytes_scanned)
            return IngestionSummary(
                run_id=run_key, source=source, scopes=tuple(scopes), rows_read=report.total_rows,
                rows_inserted=counts.inserted, rows_updated=counts.updated, rows_unchanged=counts.unchanged,
                rows_rejected=rejected, rows_deleted=counts.deleted, request_count=request_count,
                bytes_scanned=bytes_scanned, source_version=source_version,
                malformed_sample_reasons=report.sample_reasons,
            )
        except Exception as exc:
            # A failed source run never advances watermarks. Each already-started
            # period transaction remains all-or-nothing in the repository.
            self.repository.finish_run(run_key, status="failed", rows_read=len(raw_rows), counts=counts,
                rows_rejected=sum(len(report.rejected) for report in reports), source_version=source_version,
                request_count=request_count, bytes_scanned=bytes_scanned, error_summary=redact_text(exc))
            raise

    @staticmethod
    def _scopes_for_range(start_date: date, end_date: date) -> list[str]:
        scopes: list[str] = []
        cursor = start_date.replace(day=1)
        while cursor < end_date:
            scopes.append(cursor.strftime("%Y-%m"))
            cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        return scopes
