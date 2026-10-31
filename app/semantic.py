"""Semantic layer: the vocabulary the LLM may use, and the SQL built from it.

The LLM never writes SQL for normal questions. It fills a `Plan` whose fields
are enums (measures, dimensions, sort orders). This module turns a validated
plan into a parameterised query. Column names only ever come from the
whitelists below, and filter values are bound with `?`, so a plan cannot
inject SQL or touch anything except `sales_clean`.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# measure name -> SQL aggregate over sales_clean
MEASURES: dict[str, str] = {
    "revenue": "SUM(revenue)",
    "gross_revenue": "SUM(quantity * unit_price)",
    "discount": "SUM(discount_amount)",
    "quantity": "SUM(quantity)",
    "orders": "COUNT(*)",
    "avg_order_value": "AVG(revenue)",
    "distinct_products": "COUNT(DISTINCT product)",
}

MEASURE_LABELS: dict[str, str] = {
    "distinct_products": "number of different products sold",
    "revenue": "revenue",
    "gross_revenue": "gross revenue (before discounts)",
    "discount": "total discount",
    "quantity": "units sold",
    "orders": "number of orders",
    "avg_order_value": "average order value",
}

# Measures that add up across groups (so "share of total" makes sense)
ADDITIVE_MEASURES = {"revenue", "gross_revenue", "discount", "quantity", "orders"}

# dimension name -> column in sales_clean (identical names, kept explicit on purpose)
DIMENSIONS: dict[str, str] = {
    "product": "product",
    "category": "category",
    "region": "region",
    "sales_channel": "sales_channel",
    "month": "month",
}

DIMENSION_LABELS: dict[str, str] = {
    "product": "product",
    "category": "category",
    "region": "region",
    "sales_channel": "sales channel",
    "month": "month",
}

Measure = Literal[
    "revenue", "gross_revenue", "discount", "quantity", "orders", "avg_order_value", "distinct_products"
]
Dimension = Literal["product", "category", "region", "sales_channel", "month"]
Sort = Literal["value_desc", "value_asc", "label_asc"]
PlanKind = Literal["aggregate", "compare", "sql", "unsupported"]

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

SORT_SQL = {
    "value_desc": "value DESC, label ASC",
    "value_asc": "value ASC, label ASC",
    "label_asc": "label ASC",
}


class Filter(BaseModel):
    column: Dimension
    value: str = Field(min_length=1, max_length=100)


class Plan(BaseModel):
    """Structured interpretation of a question. Produced by the LLM, validated here."""

    model_config = ConfigDict(extra="ignore")

    kind: PlanKind
    measure: Measure | None = None
    group_by: Dimension | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    month_from: str | None = Field(default=None, pattern=MONTH_PATTERN)
    month_to: str | None = Field(default=None, pattern=MONTH_PATTERN)
    sort: Sort | None = None
    limit: int | None = Field(default=None, ge=1, le=50)
    period_a: str | None = Field(default=None, pattern=MONTH_PATTERN)
    period_b: str | None = Field(default=None, pattern=MONTH_PATTERN)
    sql: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("filters", mode="before")
    @classmethod
    def _none_to_empty(cls, value):
        return [] if value is None else value

    @field_validator("month_from", "month_to", "period_a", "period_b", "sql", "reason", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "Plan":
        if self.kind == "aggregate" and self.measure is None:
            raise ValueError("an aggregate plan needs a measure")
        if self.kind == "compare":
            if not (self.period_a and self.period_b):
                raise ValueError("a compare plan needs period_a and period_b")
            if self.period_a == self.period_b:
                raise ValueError("period_a and period_b must be different months")
            if self.measure is None:
                self.measure = "revenue"
        if self.kind == "sql" and not self.sql:
            raise ValueError("a sql plan needs a sql query")
        if self.month_from and self.month_to and self.month_from > self.month_to:
            raise ValueError("month_from is after month_to")
        return self

    def effective_sort(self) -> Sort:
        if self.sort:
            return self.sort
        return "label_asc" if self.group_by == "month" else "value_desc"


def build_where(
    filters: list[Filter], month_from: str | None = None, month_to: str | None = None
) -> tuple[str, list]:
    """WHERE clause from whitelisted columns; every value is a bound parameter."""
    clauses, params = [], []
    for f in filters:
        clauses.append(f"{DIMENSIONS[f.column]} = ?")
        params.append(f.value)
    if month_from:
        clauses.append("month >= ?")
        params.append(month_from)
    if month_to:
        clauses.append("month <= ?")
        params.append(month_to)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def build_aggregate_sql(plan: Plan, row_cap: int = 200) -> tuple[str, list]:
    measure_sql = MEASURES[plan.measure]
    where_sql, params = build_where(plan.filters, plan.month_from, plan.month_to)
    if plan.group_by is None:
        sql = f"SELECT {measure_sql} AS value, COUNT(*) AS orders, SUM(quantity) AS units FROM sales_clean{where_sql}"
        return sql, params
    column = DIMENSIONS[plan.group_by]
    order_sql = SORT_SQL[plan.effective_sort()]
    sql = (
        f"SELECT {column} AS label, {measure_sql} AS value, COUNT(*) AS orders "
        f"FROM sales_clean{where_sql} GROUP BY {column} ORDER BY {order_sql} LIMIT {int(row_cap)}"
    )
    return sql, params


def build_compare_sql(plan: Plan) -> tuple[str, list]:
    measure_sql = MEASURES[plan.measure]
    other_filters = [f for f in plan.filters if f.column != "month"]
    where_sql, params = build_where(other_filters)
    where_sql = (where_sql + " AND " if where_sql else " WHERE ") + "month IN (?, ?)"
    params += [plan.period_a, plan.period_b]
    sql = (
        f"SELECT month AS label, {measure_sql} AS value, COUNT(*) AS orders "
        f"FROM sales_clean{where_sql} GROUP BY month ORDER BY month"
    )
    return sql, params
