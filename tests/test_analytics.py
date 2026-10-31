"""Analytics: plans run through the engine against the real data, with exact expected figures."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.catalog import load_catalog
from app.db import connect_readonly, load_csv
from app.engine import QueryEngine
from app.semantic import Plan
from tests.conftest import write_csv


def run(engine, conn, **plan):
    return engine.run(Plan(**plan), conn)


def test_total_revenue(engine, conn):
    result = run(engine, conn, kind="aggregate", measure="revenue")
    assert result.status == "ok"
    assert (result.summary["value"], result.summary["orders"]) == (1439200, 48)
    assert "1,439,200" in result.answer


def test_distinct_products_in_a_month_also_reports_units(engine, conn):
    result = run(engine, conn, kind="aggregate", measure="distinct_products",
                 filters=[{"column": "month", "value": "2026-01"}])
    assert result.summary == {"value": 7, "orders": 8, "units": 80}
    assert result.answer == "**7** different products were sold in January 2026, totalling 80 units across 8 orders."


def test_top_product_includes_full_ranking_as_support(engine, conn):
    result = run(engine, conn, kind="aggregate", measure="revenue", group_by="product", limit=1)
    assert result.summary["label"] == "Monitor"
    assert result.summary["value"] == 473000
    assert len(result.rows) == 7  # every product, so the answer can be checked
    assert sum(r["share_pct"] for r in result.rows) == pytest.approx(100, abs=0.5)


def test_monthly_trend(engine, conn):
    result = run(engine, conn, kind="aggregate", measure="revenue", group_by="month")
    assert [(r["label"], r["value"]) for r in result.rows] == [
        ("2026-01", 210250), ("2026-02", 234600), ("2026-03", 223900),
        ("2026-04", 240700), ("2026-05", 318500), ("2026-06", 211250),
    ]
    assert result.chart["type"] == "line"
    assert "The strongest month was **May 2026** (318,500)" in result.answer
    assert "- May 2026: 318,500 (highest)" in result.answer


def test_top_region(engine, conn):
    result = run(engine, conn, kind="aggregate", measure="revenue", group_by="region", limit=1)
    assert (result.summary["label"], result.summary["value"]) == ("South", 411200)


def test_compare_two_months(engine, conn):
    result = run(engine, conn, kind="compare", measure="revenue", period_a="2026-04", period_b="2026-05")
    assert result.summary["change"] == 77800
    assert result.summary["change_pct"] == 32.3
    assert "rose by **77,800 (+32.3%)**, from 240,700 in April 2026" in result.answer


def test_filters_and_month_range(engine, conn):
    result = run(engine, conn, kind="aggregate", measure="quantity", group_by="region", limit=2,
                 filters=[{"column": "sales_channel", "value": "Online"}, {"column": "month", "value": "2026-03"}])
    assert [(r["label"], r["value"]) for r in result.rows[:2]] == [("South", 19), ("West", 5)]
    assert "?" in result.sql and "Online" in result.params  # values are bound, never pasted into SQL

    q1 = run(engine, conn, kind="aggregate", measure="revenue", month_from="2026-01", month_to="2026-03")
    assert q1.summary["value"] == 668750


def test_filter_values_are_matched_case_insensitively(engine, conn):
    result = run(engine, conn, kind="aggregate", measure="revenue",
                 filters=[{"column": "region", "value": "  south "}])
    assert result.summary["value"] == 411200
    assert result.params == ["South"]


def test_unknown_values_and_months_return_no_data(engine, conn):
    product = run(engine, conn, kind="aggregate", measure="revenue",
                  filters=[{"column": "product", "value": "Tablet"}])
    assert product.status == "no_data"
    assert "Known values" in product.answer and "Monitor" in product.answer

    month = run(engine, conn, kind="compare", period_a="2026-06", period_b="2026-07")
    assert month.status == "no_data"
    assert "July 2026" in month.answer and "January 2026 to June 2026" in month.answer


@pytest.mark.parametrize("bad_plan", [
    {"kind": "aggregate", "measure": "profit"},                               # measure not in the whitelist
    {"kind": "aggregate", "measure": "revenue", "group_by": "customer"},      # dimension not in the whitelist
    {"kind": "aggregate"},                                                    # missing measure
    {"kind": "compare", "period_a": "2026-04", "period_b": "2026-04"},        # same month twice
    {"kind": "compare", "period_a": "April", "period_b": "2026-05"},          # wrong month format
    {"kind": "aggregate", "measure": "revenue", "limit": 0},                  # out-of-range limit
])
def test_invalid_plans_are_rejected(bad_plan):
    with pytest.raises(ValidationError):
        Plan(**bad_plan)


def test_tie_for_highest_is_reported(tmp_path):
    csv_path = write_csv(tmp_path / "tie.csv", [
        "T1,01-01-2026,Desk,Furniture,North,Online,1,500,0,Completed",
        "T2,02-01-2026,Chair,Furniture,South,Online,5,100,0,Completed",
        "T3,03-01-2026,Lamp,Furniture,East,Online,1,100,0,Completed",
    ])
    load_csv(csv_path, tmp_path / "tie.db")
    conn = connect_readonly(tmp_path / "tie.db")
    engine = QueryEngine(load_catalog(conn))
    result = engine.run(Plan(kind="aggregate", measure="revenue", group_by="product", limit=1), conn)
    conn.close()
    assert result.answer.startswith("**Chair** and **Desk** are tied")
    assert set(result.summary["tied"]) == {"Chair", "Desk"}
