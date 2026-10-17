from datetime import date

import pytest

from app.contracts import ContractError, ContractRegistry, parse_contracts
from app.models import Contract
from tests.conftest import CONTRACTS_DIR

GOOD = """
name: a
version: 2
file_pattern: '^a_(\\d{8})\\.csv$'
file_date_group: 1
target_table: ds.a
columns: [{name: id, type: INT64}]
"""


def test_repo_contracts_are_valid():
    files = sorted(CONTRACTS_DIR.glob("*.yaml"))
    contracts = parse_contracts({p.name: p.read_text() for p in files})
    assert len(contracts) == len(files) == 3


@pytest.mark.parametrize(
    "file_name, contract, file_date",
    [
        ("customer_20261015.csv", "customer", date(2026, 10, 15)),
        ("some/folder/user_20261016.xlsx", "user", date(2026, 10, 16)),
        ("orders_20261015.csv", "orders", date(2026, 10, 15)),
    ],
)
def test_resolve(registry, file_name, contract, file_date):
    match = registry.resolve(file_name)
    assert match.contract.name == contract
    assert match.file_date == file_date


@pytest.mark.parametrize(
    "file_name", ["unknown_20261015.csv", "customer_2026.csv", "customer_20261015.csv.bak"]
)
def test_resolve_no_match(registry, file_name):
    assert registry.resolve(file_name) is None


def test_invalid_contract_is_skipped():
    contracts = parse_contracts({"good.yaml": GOOD, "bad.yaml": "name: b\n", "broken.yaml": "{{{"})
    assert [c.name for c in contracts] == ["a"]
    assert contracts[0].version == "2"


def test_ambiguous_match_raises():
    other = GOOD.replace("name: a", "name: a2").replace("^a_", "^a_?")
    registry = ContractRegistry(lambda: {"1": GOOD, "2": other})
    with pytest.raises(ContractError, match="multiple contracts"):
        registry.resolve("a_20261015.csv")


def test_cache_ttl():
    calls = []
    now = [0.0]

    def source():
        calls.append(1)
        return {"a.yaml": GOOD}

    registry = ContractRegistry(source, ttl_seconds=300, clock=lambda: now[0])
    registry.resolve("a_20261015.csv")
    now[0] = 299
    registry.resolve("a_20261015.csv")
    assert len(calls) == 1
    now[0] = 300
    registry.resolve("a_20261015.csv")
    assert len(calls) == 2


def test_unique_key_must_be_a_column():
    with pytest.raises(ValueError, match="unique_key"):
        Contract.model_validate(
            {"name": "x", "version": 1, "file_pattern": "x", "target_table": "d.t",
             "unique_key": ["nope"], "columns": [{"name": "id"}]}
        )  # fmt: skip


# --- by table folder: only that table's contract is read ---------------------------------------
from app.contracts import directory_contract, table_of  # noqa: E402

CUSTOMER = """
name: customer
version: 3
file_pattern: '^customer_(\\d{8})\\.(csv|xlsx)$'
file_date_group: 1
target_table: supplementary_data.customer
columns: [{name: id, type: INT64}]
"""


@pytest.mark.parametrize(
    "name, table",
    [
        ("datadesk/supplementary_data.customer/9f1c/customer_20261015.csv", "supplementary_data.customer"),
        ("supplementary_data.customer/x.csv", "supplementary_data.customer"),
        ("customer_20261015.csv", None),
        ("datadesk/9f1c/customer_20261015.csv", None),
        ("datadesk/a.b.c/x.csv", None),
    ],
)
def test_table_of(name, table):
    assert table_of(name) == table


def test_a_table_folder_reads_only_that_contract(tmp_path):
    (tmp_path / "supplementary_data.customer.yaml").write_text(CUSTOMER)
    scans = []
    fetched = []

    def source():
        scans.append(1)
        return {}

    def fetch(key):
        fetched.append(key)
        return directory_contract(tmp_path)(key)

    registry = ContractRegistry(source, fetch_one=fetch)
    match = registry.resolve("datadesk/supplementary_data.customer/9f1c/customer_20261015.csv")
    assert match.contract.version == "3" and match.file_date == date(2026, 10, 15)
    # cached: a second file for the same table doesn't read it again; nothing scans every contract
    registry.resolve("datadesk/supplementary_data.customer/a2b3/customer_20261016.csv")
    assert fetched == ["supplementary_data.customer"] and scans == []


def test_a_table_folder_without_a_contract_is_no_match(tmp_path):
    registry = ContractRegistry(lambda: {}, fetch_one=directory_contract(tmp_path))
    assert registry.resolve("datadesk/supplementary_data.unknown/1/x_20261015.csv") is None


def test_a_name_that_does_not_follow_the_pattern_still_finds_its_table(tmp_path):
    (tmp_path / "supplementary_data.customer.yaml").write_text(CUSTOMER)
    registry = ContractRegistry(lambda: {}, fetch_one=directory_contract(tmp_path))
    match = registry.resolve("datadesk/supplementary_data.customer/1/export.csv")
    assert match.contract.name == "customer" and match.file_date is None


def test_a_contract_in_the_wrong_file_is_refused(tmp_path):
    (tmp_path / "supplementary_data.orders.yaml").write_text(CUSTOMER)
    registry = ContractRegistry(lambda: {}, fetch_one=directory_contract(tmp_path))
    with pytest.raises(ContractError, match="is for supplementary_data.customer"):
        registry.resolve("supplementary_data.orders/1/orders_20261015.csv")
