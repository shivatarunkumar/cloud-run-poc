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
