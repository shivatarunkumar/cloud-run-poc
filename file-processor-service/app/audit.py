"""Writes to the audit tables.

job_metadata uses DML (INSERT, then UPDATE as the job progresses): rows written with
streaming inserts can't be UPDATEd while they sit in the streaming buffer.
validation_metadata is insert-only, so it uses streaming inserts.
"""

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from google.cloud import bigquery

from app.models import CheckRow

log = logging.getLogger(__name__)

# BigQuery parameter type per job_metadata column (STRING when not listed).
JOB_COLUMN_TYPES: dict[str, str] = {
    "delivery_attempt": "INT64",
    "attempt_number": "INT64",
    "file_size_bytes": "INT64",
    "rows_in_file": "INT64",
    "rows_loaded": "INT64",
    "rows_failed_validation": "INT64",
    "validation_rules_executed": "INT64",
    "validation_rules_failed": "INT64",
    "duration_seconds": "FLOAT64",
    "is_retried": "BOOL",
    "file_date": "DATE",
    "start_time": "TIMESTAMP",
    "end_time": "TIMESTAMP",
    "file_created_at": "TIMESTAMP",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
    "error_details": "JSON",
    "custom_labels": "JSON",
}


@dataclass
class JobRecord:
    job_id: str
    job_name: str
    created_at: datetime
    trigger_type: str = "PUBSUB"
    pubsub_message_id: str | None = None
    pubsub_subscription: str | None = None
    delivery_attempt: int | None = None
    attempt_number: int | None = None
    is_retried: bool | None = None
    status: str | None = None
    status_reason: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_seconds: float | None = None
    source_bucket: str | None = None
    file_name: str | None = None
    file_path: str | None = None
    file_generation: str | None = None
    file_size_bytes: int | None = None
    file_format: str | None = None
    file_date: date | None = None
    file_created_at: datetime | None = None
    staged_file_path: str | None = None
    final_file_path: str | None = None
    contract_name: str | None = None
    contract_version: str | None = None
    target_table: str | None = None
    load_mode: str | None = None
    bq_load_job_id: str | None = None
    rows_in_file: int | None = None
    rows_loaded: int | None = None
    rows_failed_validation: int | None = None
    validation_status: str | None = None
    validation_rules_executed: int | None = None
    validation_rules_failed: int | None = None
    recon_status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    gcp_project: str | None = None
    gcp_region: str | None = None
    service_name: str | None = None
    service_revision: str | None = None
    service_version: str | None = None
    created_by: str | None = None
    custom_labels: dict[str, Any] | None = None
    updated_at: datetime | None = None
    # Fields changed since the last write; not a column.
    _dirty: set[str] = field(default_factory=set, repr=False, compare=False)

    def set(self, **fields: Any) -> None:
        for name, value in fields.items():
            if not hasattr(self, name) or name.startswith("_"):
                raise AttributeError(f"Unknown job_metadata column: {name}")
            setattr(self, name, value)
            self._dirty.add(name)

    def columns(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}


@dataclass(frozen=True)
class PriorAttempt:
    status: str
    created_at: datetime
    final_file_path: str | None


def _param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    bq_type = JOB_COLUMN_TYPES.get(name, "STRING")
    if bq_type == "JSON":
        # Passed as STRING and wrapped in PARSE_JSON in the SQL.
        return bigquery.ScalarQueryParameter(name, "STRING", None if value is None else json.dumps(value))
    return bigquery.ScalarQueryParameter(name, bq_type, value)


def _placeholder(name: str) -> str:
    return f"PARSE_JSON(@{name})" if JOB_COLUMN_TYPES.get(name) == "JSON" else f"@{name}"


class AuditRepository:
    def __init__(
        self, client: bigquery.Client, project: str, dataset: str, job_table: str, validation_table: str
    ):
        self._client = client
        self._job_table = f"{project}.{dataset}.{job_table}"
        self._validation_table = f"{project}.{dataset}.{validation_table}"

    def _run(self, sql: str, params: list[bigquery.ScalarQueryParameter]) -> list[bigquery.Row]:
        job = self._client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
        return list(job.result())

    def find_attempts(
        self, bucket: str, name: str, generation: str, lookback_days: int = 30
    ) -> list[PriorAttempt]:
        sql = f"""
            SELECT status, created_at, final_file_path
            FROM `{self._job_table}`
            WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(lookback_days)} DAY)
              AND source_bucket = @bucket AND file_name = @name AND file_generation = @generation
        """
        rows = self._run(
            sql,
            [
                bigquery.ScalarQueryParameter("bucket", "STRING", bucket),
                bigquery.ScalarQueryParameter("name", "STRING", name),
                bigquery.ScalarQueryParameter("generation", "STRING", generation),
            ],
        )
        return [PriorAttempt(r["status"], r["created_at"], r["final_file_path"]) for r in rows]

    def insert_job(self, job: JobRecord) -> None:
        cols = job.columns()
        sql = (
            f"INSERT INTO `{self._job_table}` ({', '.join(cols)}) "
            f"VALUES ({', '.join(_placeholder(c) for c in cols)})"
        )
        self._run(sql, [_param(c, v) for c, v in cols.items()])
        job._dirty.clear()
        populated = {c: v for c, v in cols.items() if v is not None}
        log.info(
            "Audit job_metadata INSERT job_id=%s status=%s",
            job.job_id,
            job.status,
            extra={"audit_table": "job_metadata", "audit_op": "INSERT", "audit_values": populated},
        )

    def update_job(self, job: JobRecord, updated_at: datetime) -> None:
        job.set(updated_at=updated_at)
        cols = {c: v for c, v in job.columns().items() if c in job._dirty}
        assignments = ", ".join(f"{c} = {_placeholder(c)}" for c in cols)
        sql = (
            f"UPDATE `{self._job_table}` SET {assignments} "
            "WHERE job_id = @job_id AND created_at = @created_at"
        )
        params = [_param(c, v) for c, v in cols.items()]
        params += [_param("job_id", job.job_id), _param("created_at", job.created_at)]
        self._run(sql, params)
        job._dirty.clear()
        changed = {c: v for c, v in cols.items() if c != "updated_at"}
        log.info(
            "Audit job_metadata UPDATE job_id=%s %s",
            job.job_id,
            " ".join(f"{c}={v}" for c, v in changed.items() if c not in ("error_details", "custom_labels")),
            extra={"audit_table": "job_metadata", "audit_op": "UPDATE", "audit_values": changed},
        )

    def insert_validations(self, job: JobRecord, checks: list[CheckRow], validated_at: datetime) -> None:
        if not checks:
            return
        rows = [validation_row(job, c, validated_at) for c in checks]
        errors = self._client.insert_rows_json(
            self._validation_table, rows, row_ids=[r["validation_id"] for r in rows]
        )
        if errors:
            raise RuntimeError(f"validation_metadata insert failed: {errors[:3]}")
        log.info(
            "Audit validation_metadata +%d rows: %s",
            len(rows),
            ", ".join(f"{c.check_name}={c.result}" for c in checks),
            extra={
                "audit_table": "validation_metadata",
                "audit_op": "INSERT",
                "audit_values": [
                    {"check_name": c.check_name, "result": c.result, "summary": c.summary} for c in checks
                ],
            },
        )


def _json(value: Any) -> str | None:
    # JSON columns take JSON-encoded strings in streaming inserts.
    return json.dumps(value, default=str) if value else None


def validation_row(job: JobRecord, c: CheckRow, validated_at: datetime) -> dict[str, Any]:
    ts = validated_at.isoformat()
    return {
        "validation_id": str(uuid.uuid4()),
        "job_id": job.job_id,
        "file_name": job.file_name,
        "target_table": job.target_table,
        "contract_name": job.contract_name,
        "contract_version": job.contract_version,
        "stage": c.stage,
        "check_name": c.check_name,
        "check_type": str(c.check_type),
        "validation_result": c.result,
        "severity": c.severity,
        "summary": c.summary,
        "records_evaluated": c.records_evaluated,
        "records_failed": c.records_failed,
        "records_warned": c.records_warned,
        "checks_evaluated": c.checks_evaluated,
        "checks_failed": c.checks_failed,
        "failed_checks": c.failed_checks,
        "details": _json(c.details),
        "check_config": _json(c.check_config),
        "validated_at": ts,
        "created_at": ts,
    }
