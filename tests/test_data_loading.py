"""Data management: loading, cleaning, and the read-only guarantee."""
from __future__ import annotations

import sqlite3

import pytest

from app.db import DataLoadError, connect_readonly, load_csv
from tests.conftest import SALES_CSV, write_csv


def test_loads_every_row_and_flags_the_bad_ones(tmp_path):
    report = load_csv(SALES_CSV, tmp_path / "s.db")
    assert report.total_rows == 51
    assert report.invalid_rows == 2
    assert report.analytics_rows == 48
    assert report.excluded_rows == 3  # 2 invalid + 1 returned
    reasons = {issue["transaction_id"]: issue["reason"] for issue in report.issues}
    assert "Quantity must be greater than 0" in reasons["TXN0034"]
    assert "missing Unit_price" in reasons["TXN0051"]


def test_returned_order_is_stored_but_not_counted(conn):
    stored = conn.execute("SELECT status, is_valid FROM sales WHERE transaction_id = 'TXN0033'").fetchone()
    assert tuple(stored) == ("Returned", 1)
    in_clean = conn.execute("SELECT COUNT(*) FROM sales_clean WHERE transaction_id = 'TXN0033'").fetchone()[0]
    assert in_clean == 0


def test_dates_are_parsed_day_first(conn):
    row = conn.execute("SELECT order_date FROM sales WHERE transaction_id = 'TXN0001'").fetchone()
    assert row[0] == "2026-01-03"  # 03-01-2026 is 3 January, not 1 March


def test_messy_csv_edge_cases(tmp_path):
    csv_path = write_csv(tmp_path / "messy.csv", [
        "A1,01-01-2026,Desk,Furniture, North ,Online,2,100,,completed",   # blank discount, padding, lowercase status
        "A1,02-01-2026,Desk,Furniture,North,Online,1,100,0,Completed",    # duplicate id
        "A3,31-02-2026,Desk,Furniture,North,Online,1,100,0,Completed",    # impossible date
        "A4,03-01-2026,Desk,Furniture,North,Online,ten,100,0,Completed",  # non-numeric quantity
        "A5,04-01-2026,Desk,Furniture,North,Online,1,100,-5,Completed",   # negative discount
        "A6,05-01-2026,Desk,Furniture,North,Online,1,100,500,Completed",  # discount > order value
        "A7,06-01-2026,,Furniture,North,Online,1,100,0,Completed",        # missing product
    ])
    report = load_csv(csv_path, tmp_path / "messy.db")
    assert report.total_rows == 7
    assert report.analytics_rows == 1
    reasons = " | ".join(issue["reason"] for issue in report.issues)
    for expected in ("duplicate Transaction_Id", "invalid Order_date", "non-numeric Quantity",
                     "cannot be negative", "larger than the order value", "missing Product"):
        assert expected in reasons

    conn = connect_readonly(tmp_path / "messy.db")
    row = conn.execute("SELECT region, discount_amount, revenue FROM sales_clean").fetchone()
    conn.close()
    assert tuple(row) == ("North", 0.0, 200.0)


def test_missing_columns_are_rejected(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Transaction_Id,Order_date\nT1,01-01-2026\n", encoding="utf-8")
    with pytest.raises(DataLoadError, match="missing columns"):
        load_csv(bad, tmp_path / "bad.db")


def test_readonly_connection_blocks_writes(conn):
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM sales")
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 51
