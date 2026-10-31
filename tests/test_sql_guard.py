"""AI-generated SQL: only safe, read-only SELECTs over sales_clean may run."""
from __future__ import annotations

import pytest

from app.semantic import Plan
from app.sql_guard import UnsafeSQLError, validate_sql


def test_safe_select_is_accepted_and_limited():
    sql = validate_sql("SELECT product, SUM(revenue) AS r FROM sales_clean GROUP BY product")
    assert sql.endswith("LIMIT 200")


def test_ctes_over_the_allowed_view_are_accepted():
    sql = "WITH t AS (SELECT product, COUNT(DISTINCT month) AS n FROM sales_clean GROUP BY product) SELECT product FROM t"
    assert validate_sql(sql).startswith("WITH t AS")


@pytest.mark.parametrize("sql, reason", [
    ("DELETE FROM sales", "only SELECT"),
    ("DROP TABLE sales", "only SELECT"),
    ("UPDATE sales SET quantity = 0", "only SELECT"),
    ("INSERT INTO sales (transaction_id) VALUES ('X')", "only SELECT"),
    ("SELECT 1; DROP TABLE sales", "exactly one statement"),
    ("PRAGMA table_info(sales)", "only SELECT"),
    ("ATTACH DATABASE 'other.db' AS other", "only SELECT"),
    ("SELECT * FROM sales", "only sales_clean"),
    ("SELECT * FROM sqlite_master", "only sales_clean"),
    ("SELECT * FROM sales_clean WHERE product IN (SELECT product FROM sales)", "only sales_clean"),
    ("SELECT * FROM main.sales_clean", "schema-qualified"),
    ("SELECT load_extension('evil')", "not allowed"),
    ("SELECT * FROM pragma_table_info('sales')", "not allowed"),
    ("garbage text here", "could not be parsed"),
    ("this is not sql", "only SELECT"),  # parses as an expression, still refused
    ("", "empty"),
])
def test_unsafe_sql_is_rejected(sql, reason):
    with pytest.raises(UnsafeSQLError, match=reason):
        validate_sql(sql)


def test_fallback_answer_is_marked_unverified(engine, conn):
    plan = Plan(kind="sql", sql="SELECT COUNT(*) AS big_orders FROM sales_clean WHERE revenue > 50000")
    result = engine.run(plan, conn)
    assert result.status == "ok"
    assert result.verified is False
    assert result.rows == [{"big_orders": 5}]
    assert result.answer == "The answer is **5**."
    assert result.caveat.startswith("Unverified")


def test_small_fallback_results_are_stated_in_the_answer(engine, conn):
    largest = engine.run(Plan(kind="sql", sql=(
        "SELECT transaction_id, product, revenue FROM sales_clean ORDER BY revenue DESC LIMIT 1")), conn)
    assert largest.answer == (
        "Here's what I found:\n\n- **Transaction ID**: TXN0038\n- **Product**: Monitor\n- **Revenue**: 85,500"
    )

    every_month = engine.run(Plan(kind="sql", sql=(
        "SELECT product FROM sales_clean GROUP BY product HAVING COUNT(DISTINCT month) = 6 ORDER BY product")), conn)
    assert every_month.answer.startswith("I found 7 matching results:\n\n- Desk\n- Keyboard")


def test_destructive_sql_is_refused_and_data_is_untouched(engine, conn):
    result = engine.run(Plan(kind="sql", sql="DELETE FROM sales"), conn)
    assert result.status == "unsupported"
    assert "rejected" in result.answer
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 51


def test_fallback_can_be_switched_off(catalog, conn):
    from app.engine import QueryEngine

    engine = QueryEngine(catalog, sql_enabled=False)
    result = engine.run(Plan(kind="sql", sql="SELECT COUNT(*) FROM sales_clean"), conn)
    assert result.status == "unsupported"
