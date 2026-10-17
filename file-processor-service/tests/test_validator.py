from pathlib import Path

import pytest

from app.models import Contract
from app.validator import validate

CONTRACT = Contract.model_validate(
    {
        "name": "customer",
        "version": 1,
        "file_pattern": r"^customer_(\d{8})\.csv$",
        "target_table": "supplementary_data.customer",
        "unique_key": ["id"],
        "min_rows": 1,
        "columns": [
            {"name": "id", "type": "INT64", "required": True},
            {"name": "email", "type": "STRING", "required": True, "regex": r"^[^@\s]+@[^@\s]+$"},
            {"name": "country", "type": "STRING", "allowed_values": ["UK", "US"]},
            {"name": "joined", "type": "DATE"},
            {"name": "phone", "type": "STRING", "max_length": 5, "severity": "WARNING"},
        ],
    }
)
HEADER = "id,email,country,joined,phone\n"


def write(tmp_path: Path, body: str, header: str = HEADER, encoding: str = "utf-8") -> Path:
    p = tmp_path / "f.csv"
    p.write_bytes((header + body).encode(encoding))
    return p


def by_type(report, rule_type, column=None):
    return next(
        r for r in report.results if r.rule_type == rule_type and (column is None or r.column_name == column)
    )


def test_valid_file_passes(tmp_path):
    report = validate(write(tmp_path, "1,a@x.com,UK,2026-01-01,123\n2,b@x.com,,,\n"), CONTRACT)
    assert report.passed
    assert report.rows_in_file == 2
    assert report.failed_rows == set()
    assert all(r.passed for r in report.results)
    assert {r.rule_type for r in report.results} >= {
        "ENCODING", "HEADER_MATCH", "ROW_COUNT", "COLUMN_COUNT", "NOT_NULL", "TYPE", "REGEX",
        "ALLOWED_VALUES", "MAX_LENGTH", "UNIQUE",
    }  # fmt: skip


def test_header_mismatch_stops_row_checks(tmp_path):
    report = validate(
        write(tmp_path, "1,a@x.com,UK,2026-01-01\n", header="id,email,country,joined\n"), CONTRACT
    )
    assert not report.passed
    header = by_type(report, "HEADER_MATCH")
    assert "missing columns: ['phone']" in header.error_message
    assert not any(r.stage == "ROW_LEVEL" for r in report.results)
    assert report.rows_in_file == 1


def test_header_wrong_order(tmp_path):
    report = validate(write(tmp_path, "", header="email,id,country,joined,phone\n"), CONTRACT)
    assert by_type(report, "HEADER_MATCH").error_message == "columns are in the wrong order"


@pytest.mark.parametrize(
    "row, rule_type, column",
    [
        (",a@x.com,UK,,", "NOT_NULL", "id"),
        ("abc,a@x.com,UK,,", "TYPE", "id"),
        ("1,not-an-email,UK,,", "REGEX", "email"),
        ("1,a@x.com,FR,,", "ALLOWED_VALUES", "country"),
        ("1,a@x.com,UK,15/01/2026,", "TYPE", "joined"),
    ],
)
def test_row_rule_failures(tmp_path, row, rule_type, column):
    report = validate(write(tmp_path, row + "\n2,ok@x.com,US,,\n"), CONTRACT)
    assert not report.passed
    rule = by_type(report, rule_type, column)
    assert not rule.passed
    assert rule.records_failed == 1
    assert rule.sample_failures[0]["row_number"] == 1
    assert report.failed_rows == {1}


def test_duplicate_key(tmp_path):
    report = validate(write(tmp_path, "1,a@x.com,UK,,\n1,b@x.com,UK,,\n"), CONTRACT)
    unique = by_type(report, "UNIQUE")
    assert not unique.passed
    assert unique.sample_failures == [{"row_number": 2, "value": {"id": "1"}}]


def test_warning_rule_does_not_block(tmp_path):
    report = validate(write(tmp_path, "1,a@x.com,UK,,1234567\n"), CONTRACT)
    max_len = by_type(report, "MAX_LENGTH", "phone")
    assert not max_len.passed and max_len.severity == "WARNING"
    assert report.passed
    assert report.failed_rows == set()


def test_column_count(tmp_path):
    report = validate(write(tmp_path, "1,a@x.com,UK\n2,b@x.com,UK,,\n"), CONTRACT)
    cc = by_type(report, "COLUMN_COUNT")
    assert not cc.passed and cc.records_failed == 1
    assert report.failed_rows == {1}


def test_empty_file_fails_row_count(tmp_path):
    report = validate(write(tmp_path, ""), CONTRACT)
    assert not by_type(report, "ROW_COUNT").passed
    assert not report.passed


def test_completely_empty_file(tmp_path):
    report = validate(write(tmp_path, "", header=""), CONTRACT)
    assert not by_type(report, "HEADER_MATCH").passed


def test_blank_lines_are_ignored(tmp_path):
    report = validate(write(tmp_path, "1,a@x.com,UK,,\n\n,,,,\n"), CONTRACT)
    assert report.passed
    assert report.rows_in_file == 1


def test_bom_is_accepted(tmp_path):
    report = validate(write(tmp_path, "1,a@x.com,UK,,\n", encoding="utf-8-sig"), CONTRACT)
    assert report.passed


def test_invalid_encoding(tmp_path):
    report = validate(write(tmp_path, "1,café@x.com,UK,,\n", encoding="latin-1"), CONTRACT)
    assert not report.passed
    assert by_type(report, "ENCODING").error_message.startswith("File is not valid utf-8")


def test_sample_failures_are_capped(tmp_path):
    body = "".join(f"x{i},a@x.com,UK,,\n" for i in range(30))
    report = validate(write(tmp_path, body), CONTRACT, max_samples=3)
    rule = by_type(report, "TYPE", "id")
    assert rule.records_failed == 30
    assert len(rule.sample_failures) == 3


# Rules DataDesk's onboarding contracts add: per-column unique, numeric range, minimum length.
MERGED = Contract.model_validate(
    {
        "name": "orders",
        "version": 3,
        "file_pattern": r"^orders_(\d{8})\.csv$",
        "target_table": "supplementary_data.orders",
        "columns": [
            {"name": "order_id", "type": "INT64", "required": True, "unique": True},
            {"name": "amount", "type": "NUMERIC", "min": 0, "max": 1000},
            {"name": "code", "type": "STRING", "min_length": 3, "description": "Three-letter code"},
        ],
    }
)
MERGED_HEADER = "order_id,amount,code\n"


@pytest.mark.parametrize(
    "rows, rule_type, column, failed_row",
    [
        ("1,10,ABC\n1,20,DEF\n", "UNIQUE", "order_id", 2),
        ("1,-5,ABC\n2,20,DEF\n", "RANGE", "amount", 1),
        ("1,10,ABC\n2,1001,DEF\n", "RANGE", "amount", 2),
        ("1,10,AB\n2,20,DEF\n", "MIN_LENGTH", "code", 1),
    ],
)
def test_merged_rules(tmp_path, rows, rule_type, column, failed_row):
    report = validate(write(tmp_path, rows, header=MERGED_HEADER), MERGED)
    rule = by_type(report, rule_type, column)
    assert not rule.passed and rule.records_failed == 1
    assert rule.sample_failures[0]["row_number"] == failed_row
    assert rule.category == "contract"
    assert not report.passed


def test_merged_rules_pass(tmp_path):
    report = validate(write(tmp_path, "1,0,ABC\n2,1000,DEFG\n3,,XYZ\n", header=MERGED_HEADER), MERGED)
    assert report.passed
    assert {"UNIQUE", "RANGE", "MIN_LENGTH"} <= {r.rule_type for r in report.results}


def test_min_above_max_is_refused():
    with pytest.raises(ValueError, match="min is greater than max"):
        Contract.model_validate(
            {**MERGED.model_dump(), "columns": [{"name": "a", "type": "INT64", "min": 5, "max": 1}]}
        )
