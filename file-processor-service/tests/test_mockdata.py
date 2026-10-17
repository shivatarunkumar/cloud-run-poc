"""Each file in mockdata/ produces the outcome documented in project.md §7."""

import pytest

from tests.conftest import LANDING, REPO_ROOT, make_envelope

MOCKDATA = REPO_ROOT / "mockdata"

EXPECTED = {
    "customer_20261015.csv": ("SUCCESS", 20, set()),
    "user_20261015.csv": ("SUCCESS", 15, set()),
    "orders_20261015.csv": ("SUCCESS", 25, set()),
    "orders_20261016.xlsx": ("SUCCESS", 10, set()),
    "customer_20261017.csv": ("SUCCESS", 5, {"REGEX"}),  # WARNING only
    "customer_20261016.csv": ("REJECTED", None, {"REGEX", "NOT_NULL", "UNIQUE", "ALLOWED_VALUES", "TYPE"}),
    "user_20261016.csv": ("REJECTED", None, {"HEADER_MATCH"}),
    "unknown_20261015.csv": ("REJECTED", None, {"CONTRACT_MATCH"}),
}


def test_every_mock_file_is_covered():
    files = {p.name for p in MOCKDATA.iterdir() if p.suffix in (".csv", ".xlsx")}
    assert files == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_mock_file_outcome(name, processor, storage, audit):
    status, rows_loaded, failed_rules = EXPECTED[name]
    storage.put(LANDING, name, (MOCKDATA / name).read_bytes())

    outcome = processor.handle(make_envelope(name))

    assert outcome.status == status, audit.job
    assert audit.job["rows_loaded"] == rows_loaded
    assert audit.failed_rules() == failed_rules
