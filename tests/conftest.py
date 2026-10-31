"""Shared fixtures. The LLM is replaced by FakePlanner so tests are fast and deterministic."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog import load_catalog
from app.db import connect_readonly, load_csv
from app.engine import QueryEngine
from app.main import app, get_narrator, get_planner
from app.planner import PlannerError
from app.semantic import Plan

ROOT = Path(__file__).resolve().parent.parent
SALES_CSV = ROOT / "data" / "sales.csv"


class FakePlanner:
    """Stands in for Gemini: returns a fixed plan per known question."""

    def __init__(self, plans: dict):
        self.plans = plans

    def plan(self, question: str) -> Plan:
        spec = self.plans.get(question)
        if spec is None:
            return Plan(kind="unsupported", reason="Question not in the test fixtures.")
        if isinstance(spec, Exception):
            raise spec
        return Plan.model_validate(spec)


# What a correct planner should produce for each question.
PLANS = {
    "What is the total revenue?": {"kind": "aggregate", "measure": "revenue"},
    "Which product generated the highest revenue?": {
        "kind": "aggregate", "measure": "revenue", "group_by": "product", "sort": "value_desc", "limit": 1,
    },
    "What is the monthly revenue trend?": {
        "kind": "aggregate", "measure": "revenue", "group_by": "month", "sort": "label_asc",
    },
    "Which region generated the highest sales?": {
        "kind": "aggregate", "measure": "revenue", "group_by": "region", "sort": "value_desc", "limit": 1,
    },
    "How did revenue change between April and May?": {
        "kind": "compare", "measure": "revenue", "period_a": "2026-04", "period_b": "2026-05",
    },
    "How many products sold in Jan 2026": {
        "kind": "aggregate", "measure": "distinct_products", "filters": [{"column": "month", "value": "2026-01"}],
    },
    "What is the profit margin?": {
        "kind": "unsupported",
        "reason": "The data has no cost information, so profit margin can't be calculated.",
    },
    "Delete all the sales records": {"kind": "sql", "sql": "DELETE FROM sales"},
    "Compare June and July revenue": {
        "kind": "compare", "measure": "revenue", "period_a": "2026-06", "period_b": "2026-07",
    },
    "Revenue from tablets": {
        "kind": "aggregate", "measure": "revenue", "filters": [{"column": "product", "value": "Tablet"}],
    },
    "How many orders had revenue above 50,000?": {
        "kind": "sql", "sql": "SELECT COUNT(*) AS big_orders FROM sales_clean WHERE revenue > 50000",
    },
    "trigger planner failure": PlannerError("Gemini request failed: quota exceeded"),
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    load_csv(SALES_CSV, path)
    return path


@pytest.fixture
def conn(db_path: Path):
    connection = connect_readonly(db_path)
    yield connection
    connection.close()


@pytest.fixture
def catalog(conn):
    return load_catalog(conn)


@pytest.fixture
def engine(catalog) -> QueryEngine:
    return QueryEngine(catalog, sql_enabled=True)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("CSV_PATH", str(SALES_CSV))
    monkeypatch.setenv("ENABLE_SQL_FALLBACK", "true")
    app.dependency_overrides[get_planner] = lambda: FakePlanner(PLANS)
    app.dependency_overrides[get_narrator] = lambda: None  # template answers; narrator tested separately
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def write_csv(path: Path, rows: list[str]) -> Path:
    header = "Transaction_Id,Order_date,Product,Category,Region,Sales_channel,Quantity,Unit_price,Discount_amount,Status"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path
