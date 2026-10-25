"""Typed, value-redacted configuration for opt-in AWS ingestion."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from collections.abc import Mapping

from .errors import AwsIngestionDisabledError, ConfigError

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
S3_URI_RE = re.compile(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](?:/[A-Za-z0-9._!$&'()*+,;=:@%/-]*)?$")
VALID_METRICS = {"UnblendedCost", "AmortizedCost", "NetUnblendedCost", "NetAmortizedCost"}
VALID_CUR_FORMATS = {"legacy_cur", "cur2"}


@dataclass(frozen=True)
class AwsConfig:
    enabled: bool = False
    region: str = "us-east-1"
    profile: str | None = None
    cost_metric: str = "UnblendedCost"
    max_lookback_months: int = 13
    max_api_requests: int = 100
    max_pages: int = 100
    max_bytes_scanned: int = 5_000_000_000
    max_malformed_ratio: float = 0.05
    resync_window_days: int = 90
    cur_format: str = "cur2"
    athena_database: str | None = None
    athena_table: str | None = None
    athena_workgroup: str | None = None
    athena_output_location: str | None = None
    poll_timeout_seconds: int = 300
    poll_interval_seconds: float = 1.0
    max_retry_attempts: int = 4
    source_authority: str = "cur"
    tag_field_map: Mapping[str, str] = field(default_factory=lambda: {
        "team": "team", "environment": "environment", "env": "environment", "project": "project",
    })

    def require_enabled(self) -> None:
        if not self.enabled:
            raise AwsIngestionDisabledError(
                "AWS ingestion is disabled. Set AWS_INGESTION_ENABLED=true only for a deliberate sandbox run."
            )

    def require_athena(self) -> None:
        self.require_enabled()
        missing = [name for name, value in (
            ("AWS_ATHENA_DATABASE", self.athena_database),
            ("AWS_ATHENA_TABLE", self.athena_table),
            ("AWS_ATHENA_WORKGROUP", self.athena_workgroup),
            ("AWS_ATHENA_OUTPUT_LOCATION", self.athena_output_location),
        ) if not value]
        if missing:
            raise ConfigError("Missing required setting(s): " + ", ".join(missing))
        invalid = [key for key, value in (
            ("AWS_ATHENA_DATABASE", self.athena_database), ("AWS_ATHENA_TABLE", self.athena_table),
            ("AWS_ATHENA_WORKGROUP", self.athena_workgroup),
        ) if value and not IDENTIFIER_RE.fullmatch(value)]
        if self.athena_output_location and not S3_URI_RE.fullmatch(self.athena_output_location):
            invalid.append("AWS_ATHENA_OUTPUT_LOCATION")
        if invalid:
            raise ConfigError("Invalid setting(s): " + ", ".join(sorted(invalid)))


def _streamlit_secrets() -> Mapping[str, object]:
    """Read Streamlit secrets only when called; tolerate absent secrets files."""
    try:
        import streamlit as st
        return st.secrets
    except Exception:
        return {}


def load_aws_config(
    environ: Mapping[str, str] | None = None,
    secrets: Mapping[str, object] | None = None,
) -> AwsConfig:
    """Load and validate settings without creating an AWS session or client.

    Environment variables take precedence over Streamlit secrets. When the
    enable flag is false (the default), AWS-only settings remain optional.
    Invalid values are never echoed in raised errors.
    """
    env = os.environ if environ is None else environ
    secret_values = _streamlit_secrets() if secrets is None else secrets

    def get(key: str, default: object = None) -> object:
        return env.get(key, secret_values.get(key, default))

    errors: list[str] = []
    raw_enabled = str(get("AWS_INGESTION_ENABLED", "false")).strip().casefold()
    if raw_enabled not in {"true", "false", "1", "0", "yes", "no"}:
        errors.append("AWS_INGESTION_ENABLED")
    enabled = raw_enabled in {"true", "1", "yes"}

    def integer(key: str, default: int, minimum: int = 1) -> int:
        raw = get(key, default)
        try:
            value = int(str(raw))
            if value < minimum:
                raise ValueError
            return value
        except (TypeError, ValueError):
            errors.append(key)
            return default

    def text(key: str, default: str | None = None) -> str | None:
        raw = get(key, default)
        if raw is None:
            return None
        result = str(raw).strip()
        return result or None

    region = text("AWS_REGION", "us-east-1") or "us-east-1"
    profile = text("AWS_PROFILE")
    metric = str(get("AWS_COST_METRIC", "UnblendedCost")).strip()
    cur_format = str(get("AWS_CUR_FORMAT", "cur2")).strip().casefold()
    authority = str(get("AWS_SOURCE_AUTHORITY", "cur")).strip().casefold()
    if metric not in VALID_METRICS:
        errors.append("AWS_COST_METRIC")
    if cur_format not in VALID_CUR_FORMATS:
        errors.append("AWS_CUR_FORMAT")
    if authority not in {"cur", "cost_explorer"}:
        errors.append("AWS_SOURCE_AUTHORITY")
    try:
        malformed_ratio = float(str(get("AWS_MAX_MALFORMED_RATIO", "0.05")))
        if not 0 <= malformed_ratio <= 1:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("AWS_MAX_MALFORMED_RATIO")
        malformed_ratio = 0.05
    try:
        poll_interval = float(str(get("AWS_POLL_INTERVAL_SECONDS", 1.0)))
        if poll_interval < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("AWS_POLL_INTERVAL_SECONDS")
        poll_interval = 1.0

    athena_database = text("AWS_ATHENA_DATABASE")
    athena_table = text("AWS_ATHENA_TABLE")
    athena_workgroup = text("AWS_ATHENA_WORKGROUP")
    output_location = text("AWS_ATHENA_OUTPUT_LOCATION")
    if enabled:
        for key, value in (("AWS_ATHENA_DATABASE", athena_database),
                           ("AWS_ATHENA_TABLE", athena_table),
                           ("AWS_ATHENA_WORKGROUP", athena_workgroup),
                           ("AWS_ATHENA_OUTPUT_LOCATION", output_location)):
            if value and key in {"AWS_ATHENA_DATABASE", "AWS_ATHENA_TABLE", "AWS_ATHENA_WORKGROUP"} and not IDENTIFIER_RE.fullmatch(value):
                errors.append(key)
            if value and key == "AWS_ATHENA_OUTPUT_LOCATION" and not S3_URI_RE.fullmatch(value):
                errors.append(key)

    tag_map: Mapping[str, str] = {"team": "team", "environment": "environment", "env": "environment", "project": "project"}
    raw_map = get("AWS_TAG_FIELD_MAP")
    if raw_map:
        try:
            candidate = json.loads(str(raw_map))
            if not isinstance(candidate, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or v not in {"team", "environment", "project"}
                for k, v in candidate.items()
            ):
                raise ValueError
            tag_map = {k.strip().casefold(): v for k, v in candidate.items() if k.strip()}
        except (ValueError, TypeError, json.JSONDecodeError):
            errors.append("AWS_TAG_FIELD_MAP")

    if errors:
        raise ConfigError("Invalid setting(s): " + ", ".join(sorted(set(errors))))
    return AwsConfig(
        enabled=enabled, region=region, profile=profile, cost_metric=metric,
        max_lookback_months=integer("AWS_MAX_LOOKBACK_MONTHS", 13),
        max_api_requests=integer("AWS_MAX_API_REQUESTS", 100),
        max_pages=integer("AWS_MAX_PAGES", 100),
        max_bytes_scanned=integer("AWS_MAX_BYTES_SCANNED", 5_000_000_000),
        max_malformed_ratio=malformed_ratio,
        resync_window_days=integer("AWS_RESYNC_WINDOW_DAYS", 90),
        cur_format=cur_format, athena_database=athena_database, athena_table=athena_table,
        athena_workgroup=athena_workgroup, athena_output_location=output_location,
        poll_timeout_seconds=integer("AWS_POLL_TIMEOUT_SECONDS", 300),
        poll_interval_seconds=poll_interval,
        max_retry_attempts=integer("AWS_MAX_RETRY_ATTEMPTS", 4),
        source_authority=authority, tag_field_map=tag_map,
    )


def create_boto3_client(service_name: str, config: AwsConfig):
    """Create a client only after explicit opt-in; use the standard credential chain."""
    config.require_enabled()
    try:
        import boto3  # Optional dependency; deliberately lazy.
    except ImportError as exc:
        raise ConfigError("AWS client support is optional; install the package with the [aws] extra.") from exc
    session = boto3.Session(profile_name=config.profile, region_name=config.region)
    return session.client(service_name)
