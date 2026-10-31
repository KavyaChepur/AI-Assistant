"""AI integration: parsing and validating the model's output, and the context it is given."""
from __future__ import annotations

import pytest

from app.planner import PlannerError, build_system_prompt, parse_plan, response_schema


def test_valid_json_becomes_a_plan():
    plan = parse_plan('{"kind": "aggregate", "measure": "revenue", "group_by": null, "filters": null}')
    assert plan.kind == "aggregate" and plan.measure == "revenue" and plan.filters == []


def test_markdown_fenced_json_is_accepted():
    plan = parse_plan('```json\n{"kind": "unsupported", "reason": "No cost data."}\n```')
    assert plan.kind == "unsupported"


@pytest.mark.parametrize("text, message", [
    ("", "empty"),
    ("The total revenue is 1.4 million", "valid JSON"),
    ("[1, 2, 3]", "not an object"),
    ('{"kind": "aggregate", "measure": "profit"}', "invalid plan"),
    ('{"kind": "drop_tables"}', "invalid plan"),
])
def test_unusable_model_output_raises(text, message):
    with pytest.raises(PlannerError, match=message):
        parse_plan(text)


def test_prompt_contains_real_values_from_the_database(catalog):
    prompt = build_system_prompt(catalog, sql_enabled=True)
    assert "Monitor" in prompt and "USB-C Hub" in prompt
    assert "2026-01" in prompt and "2026-06" in prompt
    assert '"sql"' in prompt


def test_sql_kind_is_hidden_when_fallback_is_disabled(catalog):
    assert '"sql"' not in build_system_prompt(catalog, sql_enabled=False)
    assert "sql" not in response_schema(sql_enabled=False)["properties"]["kind"]["enum"]


def test_schema_is_accepted_by_the_gemini_sdk():
    types = pytest.importorskip("google.genai.types")
    schema = types.Schema.model_validate(response_schema(sql_enabled=True))
    assert "compare" in schema.properties["kind"].enum
