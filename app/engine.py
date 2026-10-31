"""Query engine: execute a validated plan and build the answer from the results.

Every number in an answer is read from a query result here. The wording comes
from answers.py (templates); narrator.py may later rephrase it, but only if its
text uses no number that isn't in these results.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field

from . import answers
from .catalog import Catalog, month_label
from .db import QueryTimeout, run_query
from .semantic import ADDITIVE_MEASURES, Filter, Plan, build_aggregate_sql, build_compare_sql
from .sql_guard import UnsafeSQLError, validate_sql


@dataclass
class Result:
    status: str  # "ok" | "no_data" | "unsupported"
    answer: str  # Markdown
    verified: bool = True  # False when the numbers come from AI-written SQL
    caveat: str | None = None  # shown under the answer, e.g. the unverified-SQL warning
    answer_source: str = "template"  # "template" or "llm" (rewritten by the narrator)
    sql: str | None = None
    params: list = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    chart: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _num(value):
    """Tidy a number for JSON: 473000.0 -> 473000, 1234.5678 -> 1234.57."""
    if isinstance(value, float):
        value = round(value, 2)
        return int(value) if value.is_integer() else value
    return value


class QueryEngine:
    def __init__(self, catalog: Catalog, *, sql_enabled: bool = True, row_cap: int = 200, timeout_seconds: float = 2.0):
        self.catalog = catalog
        self.sql_enabled = sql_enabled
        self.row_cap = row_cap
        self.timeout_seconds = timeout_seconds

    # ---------- entry point ----------
    def run(self, plan: Plan, conn: sqlite3.Connection) -> Result:
        if plan.kind == "unsupported":
            return Result(
                "unsupported",
                plan.reason or "That question can't be answered from the sales data.",
            )
        if plan.kind == "sql":
            if not self.sql_enabled:
                return Result(
                    "unsupported",
                    "That question needs a custom query, and AI-generated SQL is disabled on this server.",
                )
            return self._run_sql(plan, conn)

        resolved, problem = self._resolve(plan)
        if problem:
            return Result("no_data", problem)
        if resolved.kind == "compare":
            return self._run_compare(resolved, conn)
        return self._run_aggregate(resolved, conn)

    # ---------- validation against real data ----------
    def _resolve(self, plan: Plan) -> tuple[Plan | None, str | None]:
        """Map filter values to real values; reject months and values the data doesn't have."""
        months = self.catalog.months
        if not months:
            return None, "The database has no usable sales data."

        filters = []
        for f in plan.filters:
            known = self.catalog.canonical(f.column, f.value)
            if known is None:
                return None, self.catalog.describe_unknown(f.column, f.value)
            filters.append(Filter(column=f.column, value=known))

        if plan.kind == "compare":
            for month in (plan.period_a, plan.period_b):
                if month not in months:
                    return None, self.catalog.describe_unknown("month", month)

        if plan.month_from or plan.month_to:
            low = plan.month_from or months[0]
            high = plan.month_to or months[-1]
            if not any(low <= m <= high for m in months):
                return None, (
                    f"There is no sales data between {month_label(low)} and {month_label(high)}. "
                    f"The data covers {self.catalog.coverage}."
                )
        return plan.model_copy(update={"filters": filters}), None

    # ---------- aggregate ----------
    def _run_aggregate(self, plan: Plan, conn: sqlite3.Connection) -> Result:
        sql, params = build_aggregate_sql(plan, self.row_cap)
        columns, rows = run_query(conn, sql, params, self.timeout_seconds)
        scope = answers.scope_phrase(plan)
        base = {"sql": sql, "params": params, "columns": columns}

        if plan.group_by is None:
            row = rows[0] if rows else {}
            if not row or not row.get("orders"):
                return Result("no_data", f"No completed sales match that question{scope}.", **base)
            value, orders, units = _num(row["value"]), row["orders"], _num(row["units"])
            return Result("ok", answers.total_answer(plan, value, orders, units),
                          rows=[{"value": value, "orders": orders, "units": units}],
                          summary={"value": value, "orders": orders, "units": units}, **base)

        if not rows:
            return Result("no_data", f"No completed sales match that question{scope}.", **base)

        rows = [{k: _num(v) for k, v in r.items()} for r in rows]
        if plan.measure in ADDITIVE_MEASURES:
            total = sum(r["value"] or 0 for r in rows)
            for r in rows:
                r["share_pct"] = round(100 * r["value"] / total, 1) if total else None
            base["columns"] = columns + ["share_pct"]

        is_month = plan.group_by == "month"
        sort = plan.effective_sort()

        def name(r):
            return month_label(r["label"]) if is_month else r["label"]

        if plan.limit == 1 and sort in ("value_desc", "value_asc"):
            answer, summary = answers.top_answer(plan, rows, name)
        elif plan.limit and sort in ("value_desc", "value_asc"):
            answer, summary = answers.ranking_answer(plan, rows, name)
        elif is_month and sort == "label_asc":
            answer, summary = answers.trend_answer(plan, rows, name)
        else:
            answer, summary = answers.breakdown_answer(plan, rows, name)

        chart = {"type": "line" if is_month and sort == "label_asc" else "bar", "x": "label", "y": "value"}
        return Result("ok", answer, rows=rows, summary=summary, chart=chart, **base)

    # ---------- compare two months ----------
    def _run_compare(self, plan: Plan, conn: sqlite3.Connection) -> Result:
        sql, params = build_compare_sql(plan)
        columns, rows = run_query(conn, sql, params, self.timeout_seconds)
        measure = plan.measure
        scope = answers.scope_phrase(plan, include_months=False)
        by_month = {r["label"]: r for r in rows}
        base = {"sql": sql, "params": params, "columns": columns}

        def value_for(month):
            r = by_month.get(month)
            if r is not None:
                return _num(r["value"]), r["orders"]
            # No matching orders that month: additive measures are genuinely 0, averages are undefined.
            return (0, 0) if measure in ADDITIVE_MEASURES else (None, 0)

        a, orders_a = value_for(plan.period_a)
        b, orders_b = value_for(plan.period_b)
        out_rows = [
            {"label": plan.period_a, "value": a, "orders": orders_a},
            {"label": plan.period_b, "value": b, "orders": orders_b},
        ]
        if a is None or b is None:
            return Result("no_data", f"No completed sales match that question{scope} in one of those months.",
                          rows=out_rows, **base)

        change = _num(b - a)
        pct = round(100 * change / a, 1) if a else None
        summary = {"period_a": plan.period_a, "value_a": a, "period_b": plan.period_b, "value_b": b,
                   "change": change, "change_pct": pct}
        return Result("ok", answers.compare_answer(plan, a, b, change, pct), rows=out_rows, summary=summary,
                      chart={"type": "bar", "x": "label", "y": "value"}, **base)

    # ---------- AI-generated SQL fallback ----------
    def _run_sql(self, plan: Plan, conn: sqlite3.Connection) -> Result:
        try:
            safe_sql = validate_sql(plan.sql, max_rows=self.row_cap)
        except UnsafeSQLError as exc:
            return Result("unsupported", f"I couldn't answer that safely: the generated query was rejected ({exc}).",
                          verified=False, sql=plan.sql)
        try:
            columns, rows = run_query(conn, safe_sql, (), self.timeout_seconds)
        except QueryTimeout:
            return Result("unsupported", "The generated query took too long and was stopped.",
                          verified=False, sql=safe_sql)
        except sqlite3.Error as exc:
            return Result("unsupported", f"The generated query failed to run ({exc}).",
                          verified=False, sql=safe_sql)

        rows = [{k: _num(v) for k, v in r.items()} for r in rows]
        if not rows:
            return Result("no_data", "The query ran but found no matching rows.", verified=False,
                          caveat=answers.CAVEAT_SQL, sql=safe_sql, columns=columns)
        return Result("ok", answers.rows_answer(columns, rows), verified=False, caveat=answers.CAVEAT_SQL,
                      sql=safe_sql, columns=columns, rows=rows)
