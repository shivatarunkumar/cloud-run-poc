"""Pydantic models for the Pub/Sub push envelope, GCS events and data contracts."""

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Pub/Sub push envelope ---------------------------------------------------


class PubSubMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    data: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    message_id: str = Field(alias="messageId")
    publish_time: datetime | None = Field(default=None, alias="publishTime")


class PushEnvelope(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: PubSubMessage
    subscription: str | None = None
    delivery_attempt: int | None = Field(default=None, alias="deliveryAttempt")


class GcsObjectEvent(BaseModel):
    """GCS notification (JSON_API_V1): event type from attributes, object resource from data."""

    event_type: str
    bucket: str
    name: str
    generation: str
    size: int | None = None
    content_type: str | None = None
    time_created: datetime | None = None

    @classmethod
    def from_envelope(cls, envelope: PushEnvelope) -> "GcsObjectEvent":
        attrs = envelope.message.attributes
        obj: dict[str, Any] = {}
        if envelope.message.data:
            obj = json.loads(base64.b64decode(envelope.message.data))
        return cls(
            event_type=attrs.get("eventType", ""),
            bucket=obj.get("bucket") or attrs.get("bucketId", ""),
            name=obj.get("name") or attrs.get("objectId", ""),
            generation=str(obj.get("generation") or attrs.get("objectGeneration", "")),
            size=obj.get("size"),
            content_type=obj.get("contentType"),
            time_created=obj.get("timeCreated"),
        )

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket}/{self.name}"

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    @property
    def extension(self) -> str:
        return self.basename.rsplit(".", 1)[-1].lower() if "." in self.basename else ""


# --- Data contracts ----------------------------------------------------------

ColumnType = Literal["STRING", "INT64", "NUMERIC", "FLOAT64", "BOOL", "DATE", "TIMESTAMP"]
Severity = Literal["ERROR", "WARNING"]


class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: ColumnType = "STRING"
    required: bool = False
    regex: str | None = None
    allowed_values: list[str] | None = None
    max_length: int | None = None
    # strptime format for DATE/TIMESTAMP when the file doesn't use ISO 8601.
    format: str | None = None
    severity: Severity = "ERROR"

    @field_validator("allowed_values", mode="before")
    @classmethod
    def _stringify_allowed(cls, v: Any) -> Any:
        return [str(x) for x in v] if isinstance(v, list) else v

    @cached_property
    def compiled_regex(self) -> re.Pattern[str] | None:
        return re.compile(self.regex) if self.regex else None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str | None = None
    file_pattern: str
    file_date_group: int | None = None
    file_date_format: str = "%Y%m%d"
    target_table: str  # dataset.table
    load_mode: Literal["append", "overwrite"] = "append"
    file_format: list[Literal["csv", "xlsx"]] = ["csv"]
    delimiter: str = ","
    has_header: bool = True
    encoding: str = "utf-8"
    min_rows: int = 1
    max_rows: int | None = None
    unique_key: list[str] = Field(default_factory=list)
    columns: list[ColumnSpec]

    @field_validator("version", mode="before")
    @classmethod
    def _version_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("target_table")
    @classmethod
    def _check_table(cls, v: str) -> str:
        if v.count(".") != 1:
            raise ValueError("target_table must be '<dataset>.<table>'")
        return v

    @model_validator(mode="after")
    def _check_columns(self) -> "Contract":
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("duplicate column names")
        missing = set(self.unique_key) - set(names)
        if missing:
            raise ValueError(f"unique_key columns not in columns: {sorted(missing)}")
        re.compile(self.file_pattern)
        return self

    @cached_property
    def compiled_pattern(self) -> re.Pattern[str]:
        return re.compile(self.file_pattern)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def write_disposition(self) -> str:
        return "WRITE_TRUNCATE" if self.load_mode == "overwrite" else "WRITE_APPEND"

    @property
    def job_name(self) -> str:
        return f"{self.name}_load"

    def file_date(self, match: re.Match[str]) -> date | None:
        if self.file_date_group is None:
            return None
        try:
            return datetime.strptime(match.group(self.file_date_group), self.file_date_format).date()
        except (IndexError, ValueError):
            return None


# --- Results -----------------------------------------------------------------


class JobStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED"


class Stage(StrEnum):
    """Where a single rule runs (used to group results inside a check's details)."""

    FILE_LEVEL = "FILE_LEVEL"
    ROW_LEVEL = "ROW_LEVEL"
    POST_LOAD = "POST_LOAD"


@dataclass
class RuleResult:
    """Outcome of one fine-grained rule (e.g. email REGEX). Aggregated into CheckRows for the audit table."""

    stage: Stage
    rule_name: str
    rule_type: str
    severity: Severity = "ERROR"
    column_name: str | None = None
    expected_value: str | None = None
    actual_value: str | None = None
    records_evaluated: int | None = None
    records_failed: int | None = None
    passed: bool = True
    error_message: str | None = None
    sample_failures: list[dict[str, Any]] = field(default_factory=list)
    rule_params: dict[str, Any] | None = None
    # "contract": schema/column checks, rolled into the single data_contract row.
    # "custom": contract-level business rules (unique_key, ...), one row each.
    category: Literal["contract", "custom"] = "contract"

    @property
    def result(self) -> str:
        return "PASS" if self.passed else "FAIL"

    @property
    def is_blocking(self) -> bool:
        return not self.passed and self.severity == "ERROR"

    @property
    def check_label(self) -> str:
        """`column.RULE_TYPE` for column rules, `RULE_TYPE` for file-level ones."""
        return f"{self.column_name}.{self.rule_type}" if self.column_name else self.rule_type


class CheckType(StrEnum):
    DATA_CONTRACT = "DATA_CONTRACT"
    CUSTOM_RULE = "CUSTOM_RULE"
    RECONCILIATION = "RECONCILIATION"
    CONTRACT_MATCH = "CONTRACT_MATCH"
    FILE_FORMAT = "FILE_FORMAT"


@dataclass
class CheckRow:
    """One row of file_audit.validation_metadata."""

    stage: Literal["PRE_LOAD", "POST_LOAD"]
    check_name: str
    check_type: CheckType
    result: Literal["PASS", "WARN", "FAIL"]
    summary: str
    severity: Severity | None = None
    records_evaluated: int | None = None
    records_failed: int | None = None
    records_warned: int | None = None
    checks_evaluated: int = 1
    checks_failed: int = 0
    failed_checks: list[str] = field(default_factory=list)
    details: dict[str, Any] | None = None
    check_config: dict[str, Any] | None = None
