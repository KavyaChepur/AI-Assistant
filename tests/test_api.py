"""End-to-end API tests with the LLM replaced by FakePlanner."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app


def ask(client, question):
    return client.post("/ask", json={"question": question})


@pytest.mark.parametrize("question, expected", [
    ("What is the total revenue?", "1,439,200"),
    ("Which product generated the highest revenue?", "**Monitor** had the highest revenue, with **473,000**"),
    ("What is the monthly revenue trend?", "The strongest month was **May 2026** (318,500)"),
    ("Which region generated the highest sales?", "**South** had the highest revenue, with **411,200**"),
    ("How did revenue change between April and May?", "rose by **77,800 (+32.3%)**"),
    ("How many products sold in Jan 2026", "**7** different products were sold in January 2026, totalling 80 units"),
])
def test_example_questions(client, question, expected):
    response = ask(client, question)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["verified"] is True
    assert expected in body["answer"]
    assert body["answer_source"] == "template"
    assert body["sql"].startswith("SELECT") and body["rows"]  # supporting figures are returned
    assert body["data_quality"] == {"rows_loaded": 51, "rows_used": 48, "rows_excluded": 3}


def test_unsupported_question_is_refused_without_running_sql(client):
    body = ask(client, "What is the profit margin?").json()
    assert body["status"] == "unsupported"
    assert body["sql"] is None and body["rows"] == []
    assert "cost" in body["answer"]


def test_destructive_request_is_blocked(client):
    body = ask(client, "Delete all the sales records").json()
    assert body["status"] == "unsupported" and body["verified"] is False
    db = sqlite3.connect(app.state.settings.db_path)
    assert db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 51
    db.close()


def test_month_outside_the_data(client):
    body = ask(client, "Compare June and July revenue").json()
    assert body["status"] == "no_data"
    assert "July 2026" in body["answer"]


def test_unknown_product(client):
    body = ask(client, "Revenue from tablets").json()
    assert body["status"] == "no_data" and "Tablet" in body["answer"]


def test_sql_fallback_is_labelled_unverified(client):
    body = ask(client, "How many orders had revenue above 50,000?").json()
    assert body["status"] == "ok" and body["verified"] is False
    assert body["rows"] == [{"big_orders": 5}]
    assert body["caveat"].startswith("Unverified")


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "   "}, {"question": "x" * 501}, {"q": "hi"}])
def test_invalid_requests_return_422(client, payload):
    assert client.post("/ask", json=payload).status_code == 422


def test_planner_failure_returns_502(client):
    response = ask(client, "trigger planner failure")
    assert response.status_code == 502
    assert "quota exceeded" in response.json()["detail"]


def test_missing_api_key_returns_503(tmp_path, monkeypatch):
    from tests.conftest import SALES_CSV

    monkeypatch.setenv("DB_PATH", str(tmp_path / "nokey.db"))
    monkeypatch.setenv("CSV_PATH", str(SALES_CSV))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = ask(client, "What is the total revenue?")
    assert response.status_code == 503


def test_health_and_schema(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["model"]
    schema = client.get("/schema").json()
    assert schema["coverage"] == "January 2026 to June 2026"
    assert len(schema["data_quality"]["issues"]) == 2
    assert "Monitor" in schema["dimensions"]["product"]
