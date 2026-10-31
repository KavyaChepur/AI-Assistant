"""Natural-language rewrite: Gemini may reword the answer, but may not change or add numbers."""
from __future__ import annotations

from app.engine import Result
from app.main import app, get_narrator
from app.narrator import allowed_numbers, polish, unsupported_numbers
from app.semantic import Plan


class FakeNarrator:
    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.text, self.error = text, error

    def narrate(self, question, draft, rows, summary):
        if self.error:
            raise self.error
        return self.text


def top_product(engine, conn) -> Result:
    return engine.run(Plan(kind="aggregate", measure="revenue", group_by="product", limit=1), conn)


def test_number_check_accepts_numbers_from_the_data():
    allowed = allowed_numbers("Top product?", "Monitor: 473,000", [{"label": "Monitor", "value": 473000,
                                                                    "share_pct": 32.9}], {})
    assert unsupported_numbers("**Monitor** led with **473,000**, 32.9% of revenue.", allowed) == []


def test_number_check_catches_invented_or_rounded_numbers():
    allowed = allowed_numbers("Total revenue?", "Total revenue was 1,439,200.", [{"value": 1439200}], {})
    assert unsupported_numbers("Revenue was about 1.44 million.", allowed) == ["1.44"]
    assert unsupported_numbers("Revenue was 1,500,000.", allowed) == ["1,500,000"]


def test_good_rewrite_replaces_the_template(engine, conn):
    text = "**Monitor** brought in the most revenue: **473,000**, or 32.9% of all sales."
    result = polish(top_product(engine, conn), "Which product earned the most?", FakeNarrator(text))
    assert result.answer == text and result.answer_source == "llm"


def test_rewrite_with_a_wrong_number_is_discarded(engine, conn):
    result = polish(top_product(engine, conn), "Which product earned the most?",
                    FakeNarrator("Monitor earned about **480,000**, a third of all revenue."))
    assert result.answer_source == "template"
    assert result.answer.startswith("**Monitor** had the highest revenue, with **473,000**")


def test_narrator_failure_keeps_the_template(engine, conn):
    result = polish(top_product(engine, conn), "q", FakeNarrator(error=TimeoutError("slow")))
    assert result.answer_source == "template"


def test_refusals_are_never_rewritten(engine, conn):
    refusal = engine.run(Plan(kind="unsupported", reason="No cost data."), conn)
    assert polish(refusal, "Profit?", FakeNarrator("Profit was 50%.")).answer == "No cost data."


def test_api_returns_the_rewritten_answer(client):
    text = "Total revenue came to **1,439,200** across 48 completed orders."
    app.dependency_overrides[get_narrator] = lambda: FakeNarrator(text)
    body = client.post("/ask", json={"question": "What is the total revenue?"}).json()
    assert body["answer"] == text and body["answer_source"] == "llm"
