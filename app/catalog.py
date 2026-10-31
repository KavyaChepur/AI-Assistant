"""Catalog: the distinct values that actually exist in the data.

It is read from the database at startup and used twice:
- injected into the LLM prompt, so the model sees real product/region names
  (retrieval of structured metadata, no embeddings: "vectorless RAG");
- used to validate the model's filters, so an invented value is rejected
  instead of silently returning an empty result.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .semantic import DIMENSION_LABELS, DIMENSIONS


def month_label(month: str) -> str:
    """'2026-04' -> 'April 2026'."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month


@dataclass(frozen=True)
class Catalog:
    values: dict[str, list[str]]  # dimension -> sorted distinct values

    @property
    def months(self) -> list[str]:
        return self.values.get("month", [])

    @property
    def coverage(self) -> str:
        if not self.months:
            return "no data"
        return f"{month_label(self.months[0])} to {month_label(self.months[-1])}"

    def canonical(self, dimension: str, value: str) -> str | None:
        """Case- and whitespace-insensitive match to a real value, or None."""
        wanted = " ".join(value.split()).casefold()
        for known in self.values.get(dimension, []):
            if known.casefold() == wanted:
                return known
        return None

    def describe_unknown(self, dimension: str, value: str) -> str:
        label = DIMENSION_LABELS[dimension]
        if dimension == "month":
            return f"There is no sales data for {month_label(value)}. The data covers {self.coverage}."
        known = ", ".join(self.values.get(dimension, []))
        return f"There is no {label} called '{value}' in the data. Known values: {known}."

    def to_dict(self) -> dict:
        return {"dimensions": self.values, "coverage": self.coverage}


def load_catalog(conn: sqlite3.Connection) -> Catalog:
    values: dict[str, list[str]] = {}
    for dimension, column in DIMENSIONS.items():
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM sales_clean WHERE {column} IS NOT NULL ORDER BY {column}"
        ).fetchall()
        values[dimension] = [r[0] for r in rows]
    return Catalog(values=values)
