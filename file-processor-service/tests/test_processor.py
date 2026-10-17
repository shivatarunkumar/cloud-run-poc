from datetime import timedelta

from google.api_core import exceptions as gexc
from openpyxl import Workbook

from app.audit import PriorAttempt
from tests.conftest import LANDING, make_envelope

CUSTOMER_HEADER = "customer_id,first_name,last_name,email,phone,country,signup_date,is_active\n"
GOOD_CUSTOMERS = CUSTOMER_HEADER + (
    "1,Ana,Lee,ana@x.com,+44 7700 900001,UK,2026-01-01,true\n" "2,Bo,Kim,bo@x.com,,US,2026-02-01,false\n"
)
BAD_CUSTOMERS = CUSTOMER_HEADER + (
    "1,Ana,Lee,not-an-email,,UK,2026-01-01,true\n" "1,Bo,Kim,bo@x.com,,ZZ,2026-02-01,false\n"
)


def land(storage, name, data):
    storage.put(LANDING, name, data.encode() if isinstance(data, str) else data)


def test_success_loads_and_archives(processor, storage, audit, loader):
    land(storage, "customer_20261015.csv", GOOD_CUSTOMERS)

    outcome = processor.handle(make_envelope("customer_20261015.csv"))

    assert (outcome.status, outcome.http_status) == ("SUCCESS", 200)
    job = audit.job
    assert job["status"] == "SUCCESS"
    assert job["rows_in_file"] == job["rows_loaded"] == 2
    assert job["recon_status"] == "PASSED"
    assert job["validation_status"] == "PASSED"
    assert job["target_table"] == "proj.supplementary_data.customer"
    assert job["load_mode"] == "append"
    assert str(job["file_date"]) == "2026-10-15"
    assert job["final_file_path"].startswith("gs://proj-poc-archive/customer/2026/10/15/")
    assert loader.loads == [(job["staged_file_path"], "supplementary_data.customer", "WRITE_APPEND")]
    assert storage.names(LANDING) == []
    assert storage.names("proj-poc-staging") == []  # temp files cleaned up
    # One row per check, not per column rule.
    assert [(c.check_name, c.result) for c in audit.checks] == [
        ("data_contract", "PASS"),
        ("unique_key", "PASS"),
        ("recon_row_count", "PASS"),
    ]


def test_overwrite_contract_truncates(processor, storage, loader):
    land(
        storage,
        "user_20261015.csv",
        "user_id,username,email,customer_id,role,created_at,last_login_at\n"
        "1,ana.lee,ana@x.com,1,admin,2026-01-01T10:00:00Z,\n",
    )
    assert processor.handle(make_envelope("user_20261015.csv")).status == "SUCCESS"
    assert loader.loads[0][2] == "WRITE_TRUNCATE"


def test_validation_failure_rejects_whole_file(processor, storage, audit, loader):
    land(storage, "customer_20261015.csv", BAD_CUSTOMERS)

    outcome = processor.handle(make_envelope("customer_20261015.csv"))

    assert (outcome.status, outcome.http_status) == ("REJECTED", 200)
    job = audit.job
    assert job["error_code"] == "VALIDATION_FAILED"
    assert job["rows_failed_validation"] == 2
    assert job["validation_status"] == "FAILED"
    assert job["final_file_path"].startswith("gs://proj-poc-invalid/customer/")
    assert audit.failed_rules() == {"REGEX", "ALLOWED_VALUES", "UNIQUE"}
    assert loader.loads == []
    assert storage.names(LANDING) == []


def test_unmapped_file(processor, storage, audit):
    land(storage, "unknown_20261015.csv", "a,b\n1,2\n")

    outcome = processor.handle(make_envelope("unknown_20261015.csv"))

    assert outcome.status == "REJECTED"
    job = audit.job
    assert job["job_name"] == "unmapped"
    assert job["error_code"] == "NO_CONTRACT"
    assert job["final_file_path"].startswith("gs://proj-poc-invalid/_unmapped/")
    assert audit.failed_rules() == {"CONTRACT_MATCH"}


def test_xlsx_is_converted_and_loaded(processor, storage, audit, loader, tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["order_id", "customer_id", "order_date", "amount", "currency", "status"])
    ws.append([1, 1, "15/10/2026", 10.5, "GBP", "PLACED"])
    ws.append([2, 1, "16/10/2026", 3, "EUR", "SHIPPED"])
    path = tmp_path / "o.xlsx"
    wb.save(path)
    land(storage, "orders_20261016.xlsx", path.read_bytes())

    outcome = processor.handle(make_envelope("orders_20261016.xlsx"))

    assert outcome.status == "SUCCESS", audit.job
    assert audit.job["file_format"] == "xlsx"
    assert audit.job["rows_loaded"] == 2


def test_replayed_message_after_success_is_skipped(processor, storage, audit, clock):
    audit.prior = [PriorAttempt("SUCCESS", clock.t, "gs://archive/x")]
    outcome = processor.handle(make_envelope("customer_20261015.csv"))
    assert (outcome.status, outcome.http_status) == ("DUPLICATE_SKIPPED", 200)
    assert audit.jobs == {}


def test_recent_in_progress_attempt_returns_409(processor, audit, clock):
    audit.prior = [PriorAttempt("IN_PROGRESS", clock.t - timedelta(seconds=30), None)]
    assert processor.handle(make_envelope("customer_20261015.csv")).http_status == 409


def test_stale_in_progress_attempt_is_retried(processor, storage, audit, clock):
    audit.prior = [PriorAttempt("IN_PROGRESS", clock.t - timedelta(hours=1), None)]
    land(storage, "customer_20261015.csv", GOOD_CUSTOMERS)
    outcome = processor.handle(make_envelope("customer_20261015.csv"))
    assert outcome.status == "SUCCESS"
    assert audit.job["attempt_number"] == 2
    assert audit.job["is_retried"] is True


def test_transient_load_error_returns_500_and_keeps_file(processor, storage, audit, loader):
    land(storage, "customer_20261015.csv", GOOD_CUSTOMERS)
    loader.fail = gexc.ServiceUnavailable("bq down")

    outcome = processor.handle(make_envelope("customer_20261015.csv"))

    assert (outcome.status, outcome.http_status) == ("FAILED", 500)
    assert audit.job["error_code"] == "TRANSIENT"
    assert audit.job["final_file_path"] is None
    assert storage.names(LANDING) == ["customer_20261015.csv"]  # left for the retry


def test_permanent_load_error_moves_to_invalid(processor, storage, audit, loader):
    land(storage, "customer_20261015.csv", GOOD_CUSTOMERS)
    loader.fail = gexc.BadRequest("schema mismatch")

    outcome = processor.handle(make_envelope("customer_20261015.csv"))

    assert (outcome.status, outcome.http_status) == ("FAILED", 200)
    assert audit.job["error_code"] == "BadRequest"
    assert audit.job["final_file_path"].startswith("gs://proj-poc-invalid/")


def test_archive_failure_after_load_still_acks(processor, storage, audit):
    land(storage, "customer_20261015.csv", GOOD_CUSTOMERS)
    storage.fail_move = gexc.ServiceUnavailable("gcs down")

    outcome = processor.handle(make_envelope("customer_20261015.csv"))

    assert (outcome.status, outcome.http_status) == ("SUCCESS", 200)
    assert "archive move failed" in audit.job["status_reason"]


def test_recon_mismatch_marks_failed(processor, storage, audit, loader):
    land(storage, "customer_20261015.csv", GOOD_CUSTOMERS)
    loader.output_rows = 1

    outcome = processor.handle(make_envelope("customer_20261015.csv"))

    assert (outcome.status, outcome.http_status) == ("FAILED", 200)
    assert audit.job["error_code"] == "RECON_MISMATCH"
    assert audit.job["recon_status"] == "FAILED"


def test_ignored_events(processor, audit):
    assert processor.handle(make_envelope("x.csv", event_type="OBJECT_DELETE")).status == "IGNORED"
    assert processor.handle(make_envelope("folder/")).status == "IGNORED"
    assert processor.handle(make_envelope("x.csv", bucket="other")).status == "IGNORED"
    assert audit.jobs == {}


def test_disallowed_format(processor, storage, audit):
    land(storage, "customer_20261015.csv", GOOD_CUSTOMERS)
    # The registry returns its cached Contract objects, so this changes what the processor sees.
    for c in processor.contracts.contracts():
        if c.name == "customer":
            c.file_format = ["xlsx"]
    outcome = processor.handle(make_envelope("customer_20261015.csv"))
    assert outcome.status == "REJECTED"
    assert audit.job["error_code"] == "UNSUPPORTED_FORMAT"
