"""Redact credential-shaped values and account IDs from logs and errors."""
from __future__ import annotations

import logging
import re

_KEY_ID_PREFIX = "A" + "KIA|A" + "SIA"
_KEY_ID = re.compile(r"\b(?:" + _KEY_ID_PREFIX + r")[A-Z0-9]{16}\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(aws_access_key_id|access_key_id|aws_" + "secret" + r"_access_key|secret_access_key|"
    r"aws_" + "session" + r"_token|session_token|security_token)\b\s*[:=]\s*(['\"]?)([^\s,;}'\"]+)\2"
)
_ACCOUNT_ARN = re.compile(r"\barn:(aws[a-zA-Z-]*)?:([^:\s]+):([^:\s]*):(\d{12}):")


def redact_text(value: object) -> str:
    """Return safe text with access keys, secret assignments and ARN accounts masked."""
    text = str(value)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _KEY_ID.sub("[REDACTED_ACCESS_KEY]", text)
    return _ACCOUNT_ARN.sub(lambda m: f"arn:{m.group(1) or 'aws'}:{m.group(2)}:{m.group(3)}:[REDACTED_ACCOUNT]:", text)


class RedactingFilter(logging.Filter):
    """Logging filter that sanitizes rendered messages before a handler emits them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


def install_redaction_filter(logger: logging.Logger | None = None) -> RedactingFilter:
    """Install the redactor on one logger; does not configure global logging."""
    target = logger or logging.getLogger("cloud_cost_analytics.ingestion.aws")
    filt = RedactingFilter()
    target.addFilter(filt)
    return filt
