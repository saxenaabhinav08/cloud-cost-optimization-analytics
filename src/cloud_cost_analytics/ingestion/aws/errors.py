"""Safe, actionable errors for the optional AWS ingestion code."""


class AwsIngestionError(RuntimeError):
    """Base error whose message must never contain credential material."""


class AwsIngestionDisabledError(AwsIngestionError):
    """Raised when a client or ingestion run is requested while disabled."""


class ConfigError(AwsIngestionError, ValueError):
    """Configuration is missing or invalid; messages name keys, not values."""


class AwsAccessDeniedError(AwsIngestionError):
    """The configured identity lacks an action required by the selected source."""


class AwsThrottledError(AwsIngestionError):
    """A retryable service throttle persisted after the bounded retry policy."""


class AwsRequestError(AwsIngestionError):
    """An AWS request failed for a reason other than access denial or throttling."""


class RequestBudgetExceededError(AwsIngestionError):
    """The run reached its configured Cost Explorer request budget."""


class ScanLimitExceededError(AwsIngestionError):
    """An Athena query scanned more bytes than the configured client limit."""


class PollTimeoutError(AwsIngestionError):
    """An Athena query did not finish before the configured poll timeout."""


class MalformedRatioExceededError(AwsIngestionError):
    """A batch had more rejected records than the configured malformed ratio."""


class MixedCurrencyError(AwsIngestionError, ValueError):
    """A batch contains more than one currency and cannot be safely aggregated."""
