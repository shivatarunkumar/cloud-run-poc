from app.checks import data_contract_row, gate_row, recon_row, validation_rows
from app.models import CheckType
from app.validator import validate
from tests.conftest import REPO_ROOT

MOCKDATA = REPO_ROOT / "mockdata"


def test_valid_file_is_two_pre_load_rows(registry):
    contract = registry.resolve("customer_20261015.csv").contract
    report = validate(MOCKDATA / "customer_20261015.csv", contract)

    rows = validation_rows(report, contract)

    assert [(r.check_name, r.check_type, r.result) for r in rows] == [
        ("data_contract", CheckType.DATA_CONTRACT, "PASS"),
        ("unique_key", CheckType.CUSTOM_RULE, "PASS"),
    ]
    dc = rows[0]
    assert dc.records_evaluated == 20
    assert dc.records_failed == dc.records_warned == dc.checks_failed == 0
    assert dc.checks_evaluated == len([r for r in report.results if r.category == "contract"])
    assert dc.severity is None
    # Passing checks are compact: no expected values or samples.
    assert dc.details["columns"]["email"]["regex"] == {"result": "PASS", "evaluated": 20}
    assert [c["name"] for c in dc.check_config["columns"]] == contract.column_names


def test_warning_only_is_warn(registry):
    contract = registry.resolve("customer_20261017.csv").contract
    dc = data_contract_row(validate(MOCKDATA / "customer_20261017.csv", contract), contract)
    assert (dc.result, dc.severity) == ("WARN", "WARNING")
    assert dc.failed_checks == ["phone.REGEX"]
    assert dc.records_failed == 0 and dc.records_warned == 2
    regex = dc.details["columns"]["phone"]["regex"]
    assert regex["severity"] == "WARNING"
    assert [s["value"] for s in regex["samples"]] == ["call me", "12"]


def test_failed_file_summary_and_samples(registry):
    contract = registry.resolve("customer_20261016.csv").contract
    report = validate(MOCKDATA / "customer_20261016.csv", contract)
    dc, unique = validation_rows(report, contract)

    assert dc.result == "FAIL"
    assert dc.summary.startswith("5 of 19 contract checks failed: customer_id.NOT_NULL (1 row)")
    assert dc.records_failed == 5
    assert dc.details["columns"]["country"]["allowed_values"]["samples"] == [{"row_number": 7, "value": "ZZ"}]
    assert unique.failed_checks == ["customer_id.UNIQUE"]
    assert unique.check_config == {"unique_key": ["customer_id"]}


def test_header_mismatch_is_one_failed_check(registry):
    contract = registry.resolve("user_20261016.csv").contract
    dc = data_contract_row(validate(MOCKDATA / "user_20261016.csv", contract), contract)
    assert dc.failed_checks == ["HEADER_MATCH"]
    assert "missing columns: ['role']" in dc.details["file"]["header_match"]["error"]


def test_recon_and_gate_rows():
    ok, bad = recon_row(20, 20), recon_row(20, 19)
    assert (ok.result, ok.stage, ok.summary) == ("PASS", "POST_LOAD", "20 rows loaded = 20 rows in file")
    assert (bad.result, bad.records_failed, bad.failed_checks) == ("FAIL", 1, ["RECON_ROW_COUNT"])

    gate = gate_row("contract_match", CheckType.CONTRACT_MATCH, "a contract", "x.csv", "No contract")
    assert (gate.result, gate.summary, gate.failed_checks) == ("FAIL", "No contract", ["CONTRACT_MATCH"])
