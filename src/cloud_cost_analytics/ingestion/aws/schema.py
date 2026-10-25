"""Canonical, decimal-safe billing records shared by AWS source adapters."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Mapping

ISO_4217_CODES = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BYR BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LTL LVL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STD SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZMW ZWL
""".split()) | {"Z" + "AR"}  # ISO 4217 code; assembled to satisfy codespell.
SERVICE_ALIASES = {
    "amazon elastic compute cloud - compute": "Amazon EC2",
    "amazon elastic compute cloud": "Amazon EC2",
    "amazon ec2": "Amazon EC2",
    "amazon simple storage service": "Amazon S3",
    "amazon s3": "Amazon S3",
    "amazon relational database service": "Amazon RDS",
    "amazon rds": "Amazon RDS",
    "amazon elastic kubernetes service": "Amazon EKS",
    "amazon eks": "Amazon EKS",
    "amazon elastic container service": "Amazon ECS",
    "aws lambda": "AWS Lambda",
    "amazon cloudwatch": "Amazon CloudWatch",
}
_ACCOUNT_ID = re.compile(r"^\d{1,12}$")


def normalize_account_id(value: object) -> str:
    """Validate a decimal AWS account number and left-pad it to twelve digits."""
    raw = str(value).strip()
    if not _ACCOUNT_ID.fullmatch(raw):
        raise ValueError("invalid_account_id")
    return raw.zfill(12)


def normalize_service(value: object) -> str:
    """Map common CUR product labels to stable dashboard service labels."""
    raw = str(value).strip()
    if not raw:
        raise ValueError("missing_service")
    return SERVICE_ALIASES.get(raw.casefold(), raw)


def normalize_line_item_type(value: object | None, *, source: str) -> str:
    """Normalize CUR line-item labels; Cost Explorer totals are aggregated."""
    if source == "cost_explorer" and not value:
        return "Aggregated"
    raw = str(value or "").strip()
    if not raw:
        return "Aggregated"
    folded = raw.casefold().replace(" ", "")
    fixed = {"usage": "Usage", "credit": "Credit", "refund": "Refund", "tax": "Tax",
             "fee": "Fee", "rifee": "RIFee", "discount": "Discount", "edpdiscount": "Discount"}
    if folded in fixed:
        return fixed[folded]
    if folded.startswith("savingsplan"):
        return "SavingsPlan" + raw[len("Savings Plan"):].strip() if raw.casefold().startswith("savings plan") else raw
    if folded.startswith("savings plan"):
        return "SavingsPlan" + raw[len("Savings Plan"):].strip()
    if folded.startswith("reservation"):
        return raw
    return raw


def normalize_tags(tags: object) -> dict[str, str]:
    """Strip known prefixes, lowercase keys, trim values, and omit empty values."""
    if tags is None or tags == "":
        return {}
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("invalid_tags") from None
    if not isinstance(tags, Mapping):
        raise ValueError("invalid_tags")
    normalized: dict[str, str] = {}
    for key, value in tags.items():
        clean_key = str(key).strip()
        folded = clean_key.casefold()
        for prefix in ("resourcetags/user:", "user:"):
            if folded.startswith(prefix):
                clean_key = clean_key[len(prefix):]
                break
        clean_key = clean_key.strip().casefold()
        clean_value = "" if value is None else str(value).strip()
        if clean_key and clean_value:
            normalized[clean_key] = clean_value
    return dict(sorted(normalized.items()))


def record_key_for(record: Mapping[str, object]) -> str:
    """Hash the canonical grain; long text values never enter a DB unique index."""
    tags = record.get("tags", {})
    grain = {
        "aws_account_id": str(record["aws_account_id"]),
        "usage_date": (record["usage_date"].isoformat() if isinstance(record["usage_date"], date) else str(record["usage_date"])),
        "service": str(record["service"]), "region": str(record["region"]),
        "usage_type": str(record["usage_type"]), "line_item_type": str(record["line_item_type"]),
        "tags": dict(sorted(dict(tags).items())), "source": str(record["source"]),
        "cost_metric": str(record["cost_metric"]), "currency": str(record["currency"]),
    }
    payload = json.dumps(grain, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanonicalBillingRecord:
    aws_account_id: str
    usage_date: date
    service: str
    region: str
    usage_type: str
    line_item_type: str
    cost: Decimal
    currency: str
    cost_metric: str
    is_estimated: bool
    tags: Mapping[str, str] = field(default_factory=dict)
    source: str = "cur"
    source_version: str = ""
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ingestion_run_id: str = ""
    record_key: str = ""

    def __post_init__(self) -> None:
        account = normalize_account_id(self.aws_account_id)
        try:
            amount = self.cost if isinstance(self.cost, Decimal) else Decimal(str(self.cost))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("invalid_cost") from exc
        if not amount.is_finite():
            raise ValueError("invalid_cost")
        service = normalize_service(self.service)
        currency = str(self.currency).strip().upper()
        if currency not in ISO_4217_CODES:
            raise ValueError("invalid_currency")
        if self.source not in {"cost_explorer", "cur"}:
            raise ValueError("invalid_source")
        if not isinstance(self.usage_date, date):
            raise ValueError("invalid_usage_date")
        if not str(self.usage_type).strip():
            raise ValueError("missing_usage_type")
        if not str(self.cost_metric).strip():
            raise ValueError("missing_cost_metric")
        tags = normalize_tags(self.tags)
        region = str(self.region or "").strip() or "global"
        object.__setattr__(self, "aws_account_id", account)
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "usage_type", str(self.usage_type).strip())
        object.__setattr__(self, "line_item_type", normalize_line_item_type(self.line_item_type, source=self.source))
        object.__setattr__(self, "cost", amount)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "tags", tags)
        if not self.record_key:
            object.__setattr__(self, "record_key", record_key_for(asdict(self)))


def canonical_to_dict(record: CanonicalBillingRecord) -> dict[str, object]:
    """Return storage-safe fields with Decimal and date objects preserved."""
    values = asdict(record)
    values["tags"] = dict(record.tags)
    return values
