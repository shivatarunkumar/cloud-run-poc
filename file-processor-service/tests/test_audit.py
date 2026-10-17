import json
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from app.audit import AuditRepository, JobRecord, validation_row
from app.models import CheckRow, CheckType, JobStatus
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def repo():
    client = MagicMock()
    client.insert_rows_json.return_value = []
    return client, AuditRepository(client, "proj", "file_audit", "job_metadata", "validation_metadata")


def sent(client):
    sql = client.query.call_args.args[0]
    params = client.query.call_args.kwargs["job_config"].query_parameters
    # Serializing proves every value has a type BigQuery accepts.
    return sql, {p.name: p.to_api_repr() for p in params}


def job() -> JobRecord:
    return JobRecord(
        job_id="j1",
        job_name="customer_load",
        created_at=NOW,
        status=JobStatus.IN_PROGRESS,
        file_date=date(2026, 10, 15),
        attempt_number=1,
        is_retried=False,
    )


def test_insert_job_sends_every_column_with_types():
    client, audit = repo()
    audit.insert_job(job())
    sql, params = sent(client)
    assert sql.startswith("INSERT INTO `proj.file_audit.job_metadata` (job_id, job_name, created_at,")
    assert "PARSE_JSON(@error_details)" in sql
    assert params["file_date"]["parameterType"]["type"] == "DATE"
    assert params["attempt_number"]["parameterType"]["type"] == "INT64"
    assert params["status"]["parameterValue"]["value"] == "IN_PROGRESS"
    assert params["error_details"]["parameterType"]["type"] == "STRING"


def test_update_job_sends_only_changed_columns():
    client, audit = repo()
    j = job()
    audit.insert_job(j)
    j.set(status=JobStatus.SUCCESS, rows_loaded=2, error_details={"a": 1})
    audit.update_job(j, NOW)
    sql, params = sent(client)
    assert sql.startswith("UPDATE `proj.file_audit.job_metadata` SET ")
    assert "error_details = PARSE_JSON(@error_details)" in sql
    assert sql.endswith("WHERE job_id = @job_id AND created_at = @created_at")
    assert set(params) == {"status", "rows_loaded", "error_details", "updated_at", "job_id", "created_at"}
    assert json.loads(params["error_details"]["parameterValue"]["value"]) == {"a": 1}


def test_insert_validations_rows():
    client, audit = repo()
    check = CheckRow(
        stage="PRE_LOAD",
        check_name="data_contract",
        check_type=CheckType.DATA_CONTRACT,
        result="FAIL",
        summary="1 of 19 contract checks failed: email.REGEX (1 row)",
        severity="ERROR",
        records_evaluated=4,
        records_failed=1,
        records_warned=0,
        checks_evaluated=19,
        checks_failed=1,
        failed_checks=["email.REGEX"],
        details={"columns": {"email": {"regex": {"result": "FAIL", "samples": [{"row_number": 2}]}}}},
        check_config={"columns": [{"name": "email"}]},
    )
    audit.insert_validations(job(), [check], NOW)
    table, rows = client.insert_rows_json.call_args.args[:2]
    assert table == "proj.file_audit.validation_metadata"
    row = rows[0]
    assert row["check_type"] == "DATA_CONTRACT"
    assert row["validation_result"] == "FAIL"
    assert row["failed_checks"] == ["email.REGEX"]
    assert json.loads(row["details"])["columns"]["email"]["regex"]["result"] == "FAIL"
    json.dumps(rows)  # streaming insert payload must be JSON-serializable


def test_validation_row_matches_table_schema():
    schema = json.loads((REPO_ROOT / "Infra" / "bq" / "validation_metadata.json").read_text())
    check = CheckRow(
        stage="PRE_LOAD", check_name="x", check_type=CheckType.CUSTOM_RULE, result="PASS", summary=""
    )
    assert list(validation_row(job(), check, NOW)) == [c["name"] for c in schema]


def test_job_record_matches_table_schema():
    schema = json.loads((REPO_ROOT / "Infra" / "bq" / "job_metadata.json").read_text())
    assert set(job().columns()) == {c["name"] for c in schema}
