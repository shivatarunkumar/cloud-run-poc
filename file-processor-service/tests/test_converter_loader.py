import csv
from datetime import UTC, date, datetime

import pytest
from openpyxl import Workbook

from app.contracts import ContractError
from app.converter import xlsx_to_csv
from app.loader import write_enriched_csv
from tests.conftest import TABLE_COLUMNS


def test_xlsx_to_csv(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["order_id", "order_date", "amount", "note"])
    ws.append([1, datetime(2026, 10, 15), 12.5, "a"])
    ws.append([2, date(2026, 10, 16), 3.0, None])  # trailing NULL
    ws.append([None, None, None, None])  # blank row
    ws.append([3, datetime(2026, 10, 17, 9, 30), True, "b"])
    src, dst = tmp_path / "f.xlsx", tmp_path / "f.csv"
    wb.save(src)

    assert xlsx_to_csv(src, dst) == 4
    rows = list(csv.reader(dst.open()))
    assert rows == [
        ["order_id", "order_date", "amount", "note"],
        ["1", "2026-10-15", "12.5", "a"],
        ["2", "2026-10-16", "3", ""],
        ["3", "2026-10-17 09:30:00", "true", "b"],
    ]


def test_write_enriched_csv_orders_by_table_and_normalizes(tmp_path, registry):
    contract = registry.resolve("orders_20261015.csv").contract
    src = tmp_path / "in.csv"
    src.write_text(
        "order_id,customer_id,order_date,amount,currency,status\n"
        " 7 ,1,15/10/2026,10.50,GBP,PLACED\n"
        "\n"
        "8,2,2026-10-16,3,USD,SHIPPED\n"
    )
    dst = tmp_path / "out.csv"
    table = TABLE_COLUMNS["supplementary_data.orders"]
    loaded_at = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    assert write_enriched_csv(src, dst, contract, table, "job-1", "gs://b/orders.csv", loaded_at) == 2
    rows = list(csv.reader(dst.open()))
    assert rows[0] == table
    assert rows[1] == [
        "7", "1", "2026-10-15", "10.50", "GBP", "PLACED",
        "job-1", "gs://b/orders.csv", "2026-10-15 09:00:00+00:00",
    ]  # fmt: skip
    assert rows[2][:3] == ["8", "2", "2026-10-16"]


def test_table_columns_missing_from_file_are_null(tmp_path, registry):
    contract = registry.resolve("orders_20261015.csv").contract
    src = tmp_path / "in.csv"
    src.write_text("order_id,customer_id,order_date,amount,currency,status\n1,1,15/10/2026,1,GBP,PLACED\n")
    table = ["order_id", "extra_col", *TABLE_COLUMNS["supplementary_data.orders"][1:]]
    write_enriched_csv(src, tmp_path / "o.csv", contract, table, "j", "u", datetime.now(UTC))
    assert list(csv.reader((tmp_path / "o.csv").open()))[1][:2] == ["1", ""]


def test_contract_column_not_in_table_is_an_error(tmp_path, registry):
    contract = registry.resolve("orders_20261015.csv").contract
    with pytest.raises(ContractError, match="not in table"):
        write_enriched_csv(
            tmp_path / "x", tmp_path / "y", contract, ["order_id"], "j", "u", datetime.now(UTC)
        )
