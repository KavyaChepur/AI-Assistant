"""Answer templates: turn query results into readable sentences and lists.

These always run. They are the guaranteed-correct answer, and the fallback
whenever the optional LLM rewrite (narrator.py) is off or fails its number check.
Output is Markdown: short sentences, numbered lists for rankings, bullets for breakdowns.
"""
from __future__ import annotations

from .catalog import month_label
from .semantic import DIMENSION_LABELS, Plan

PLURALS = {
    "product": "products",
    "category": "categories",
    "region": "regions",
    "sales_channel": "sales channels",
    "month": "months",
}

# How each measure reads inside a sentence
NOUNS = {
    "revenue": "revenue",
    "gross_revenue": "gross revenue (before discounts)",
    "discount": "total discount",
    "quantity": "units sold",
    "orders": "number of orders",
    "avg_order_value": "average order value",
    "distinct_products": "number of different products sold",
}

# Unit words appended to amounts, singular form
UNITS = {"quantity": "unit", "orders": "order", "distinct_products": "product"}

# (most, least) phrasing for rankings where "highest X" reads badly
RANK_VERBS = {
    "quantity": ("sold the most units", "sold the fewest units"),
    "orders": ("had the most orders", "had the fewest orders"),
    "distinct_products": ("sold the widest range of products", "sold the narrowest range of products"),
}

CAVEAT_SQL = "Unverified: this answer comes from AI-generated SQL, so please check the query below."


# ---------- small helpers ----------
def fmt(value, measure: str | None = None) -> str:
    if value is None:
        return "n/a"
    if not isinstance(value, (int, float)):
        return str(value)
    if measure in UNITS or float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def amount(value, measure: str) -> str:
    """473000 -> '473,000'; 228 units -> '228 units'; 1 order -> '1 order'."""
    text = fmt(value, measure)
    unit = UNITS.get(measure)
    if unit:
        text += f" {unit}" if value == 1 else f" {unit}s"
    return text


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def share(row: dict) -> str:
    return f" ({row['share_pct']}% of the total)" if row.get("share_pct") is not None else ""


def scope_phrase(plan: Plan, include_months: bool = True) -> str:
    """Filters as natural words: ' in the South region through the Online channel in March 2026'."""
    parts = []
    for f in plan.filters:
        if f.column == "product":
            parts.append(f"for {f.value}")
        elif f.column == "category":
            parts.append(f"in the {f.value} category")
        elif f.column == "region":
            parts.append(f"in the {f.value} region")
        elif f.column == "sales_channel":
            parts.append(f"through the {f.value} channel")
        elif f.column == "month" and include_months:
            parts.append(f"in {month_label(f.value)}")
    if include_months:
        if plan.month_from and plan.month_to:
            parts.append(f"from {month_label(plan.month_from)} to {month_label(plan.month_to)}")
        elif plan.month_from:
            parts.append(f"from {month_label(plan.month_from)} onwards")
        elif plan.month_to:
            parts.append(f"up to {month_label(plan.month_to)}")
    return (" " + " ".join(parts)) if parts else ""


def pretty_column(name: str) -> str:
    words = ["ID" if w.lower() == "id" else w.lower() for w in name.replace("_", " ").split()]
    return cap(" ".join(words)) if words else name


# ---------- one answer shape per function ----------
def total_answer(plan: Plan, value, orders: int, units) -> str:
    m, s = plan.measure, scope_phrase(plan)
    v = fmt(value, m)
    order_text = amount(orders, "orders")
    if m == "distinct_products":
        return f"**{v}** different products were sold{s}, totalling {amount(units, 'quantity')} across {order_text}."
    if m == "orders":
        return f"There were **{v}** completed orders{s}."
    if m == "quantity":
        return f"**{amount(value, m)}** were sold{s}, across {order_text}."
    if m == "avg_order_value":
        return f"The average order value{s} was **{v}**, across {order_text}."
    noun = "total revenue" if m == "revenue" else NOUNS[m]
    return f"{cap(noun)}{s} was **{v}**, from {orders:,} completed order{'' if orders == 1 else 's'}."


def top_answer(plan: Plan, rows: list[dict], name) -> tuple[str, dict]:
    """Single winner (limit 1), with a tie check and the runners-up for context."""
    m, s = plan.measure, scope_phrase(plan)
    most = plan.effective_sort() == "value_desc"
    verb = RANK_VERBS.get(m, (f"had the highest {NOUNS[m]}", f"had the lowest {NOUNS[m]}"))[0 if most else 1]
    best = rows[0]
    tied = [r for r in rows if r["value"] == best["value"]]
    summary = {"label": best["label"], "value": best["value"], "tied": [r["label"] for r in tied]}

    if len(tied) > 1:
        names = join_words([f"**{name(r)}**" for r in tied])
        return f"{names} are tied: each {verb}{s}, with **{amount(best['value'], m)}**.", summary

    text = f"**{name(best)}** {verb}{s}, with **{amount(best['value'], m)}**{share(best)}."
    others = rows[1:3]
    if others:
        listed = join_words([f"{name(r)} ({amount(r['value'], m)})" for r in others])
        if most:
            text += f" {'Next were' if len(others) > 1 else 'Next was'} {listed}."
        else:
            text += f" The next lowest {'were' if len(others) > 1 else 'was'} {listed}."
    return text, summary


def ranking_answer(plan: Plan, rows: list[dict], name) -> tuple[str, dict]:
    """Top or bottom N (limit > 1): a sentence for two items, a numbered list for more."""
    m, s, dim = plan.measure, scope_phrase(plan), plan.group_by
    most = plan.effective_sort() == "value_desc"
    top = rows[: plan.limit]
    summary = {"labels": [r["label"] for r in top]}
    which = "top" if most else "bottom"
    fewer = f"Only {len(top)} {PLURALS[dim]} have sales{s}. " if len(top) < plan.limit else ""

    if len(top) <= 2:
        listed = join_words([f"**{name(r)}** ({amount(r['value'], m)})" for r in top])
        if len(top) == 1:
            return f"{fewer}The {which} {DIMENSION_LABELS[dim]} by {NOUNS[m]}{s} was {listed}.", summary
        return f"{fewer}The {which} 2 {PLURALS[dim]} by {NOUNS[m]}{s} were {listed}.", summary

    lines = "\n".join(f"{i}. **{name(r)}**: {amount(r['value'], m)}{share(r)}" for i, r in enumerate(top, 1))
    return f"{fewer}The {which} {len(top)} {PLURALS[dim]} by {NOUNS[m]}{s} were:\n\n{lines}", summary


def breakdown_answer(plan: Plan, rows: list[dict], name) -> tuple[str, dict]:
    """A full breakdown by a dimension (no limit)."""
    m, s, dim = plan.measure, scope_phrase(plan), plan.group_by
    shown = rows[: plan.limit] if plan.limit else rows

    if len(shown) <= 2:
        parts = [f"**{amount(r['value'], m)}** for {name(r)}{share(r)}" for r in shown]
        return f"{cap(NOUNS[m])}{s} was {join_words(parts)}.", {}

    sort = plan.effective_sort()
    lead = f"Here is the {NOUNS[m]}{s} by {DIMENSION_LABELS[dim]}:"
    if sort in ("value_desc", "value_asc"):
        most = sort == "value_desc"
        verb = RANK_VERBS.get(m, (f"had the highest {NOUNS[m]}", f"had the lowest {NOUNS[m]}"))[0 if most else 1]
        first = shown[0]
        lead = (f"**{name(first)}** {verb}{s}, with **{amount(first['value'], m)}**. "
                f"Here is the full breakdown by {DIMENSION_LABELS[dim]}:")
    lines = "\n".join(f"- **{name(r)}**: {amount(r['value'], m)}{share(r)}" for r in shown)
    return f"{lead}\n\n{lines}", {}


def trend_answer(plan: Plan, rows: list[dict], name) -> tuple[str, dict]:
    """Month-by-month series: the overall movement, the best and worst months, then the list."""
    m, s = plan.measure, scope_phrase(plan)
    noun = cap(NOUNS[m])
    if len(rows) == 1:
        only = rows[0]
        return f"{noun}{s} was **{amount(only['value'], m)}** in {name(only)}, the only month with data.", {}

    first, last = rows[0], rows[-1]
    peak = max(rows, key=lambda r: r["value"])
    low = min(rows, key=lambda r: r["value"])
    change = (last["value"] or 0) - (first["value"] or 0)
    if change > 0:
        movement = "rose"
    elif change < 0:
        movement = "fell"
    else:
        movement = "ended where it started,"
    pct = f" ({100 * change / first['value']:+.1f}%)" if first["value"] and change else ""

    lead = (f"{noun}{s} {movement} from {fmt(first['value'], m)} in {name(first)} "
            f"to {fmt(last['value'], m)} in {name(last)}{pct}. "
            f"The strongest month was **{name(peak)}** ({amount(peak['value'], m)}) "
            f"and the weakest was **{name(low)}** ({amount(low['value'], m)}).")

    def tag(r):
        if r is peak:
            return " (highest)"
        if r is low:
            return " (lowest)"
        return ""

    lines = "\n".join(f"- {name(r)}: {amount(r['value'], m)}{tag(r)}" for r in rows)
    return f"{lead}\n\n{lines}", {"peak": peak["label"], "low": low["label"]}


def compare_answer(plan: Plan, a, b, change, pct) -> str:
    m, s = plan.measure, scope_phrase(plan, include_months=False)
    name_a, name_b = month_label(plan.period_a), month_label(plan.period_b)
    noun = cap(NOUNS[m])
    if change == 0:
        return f"{noun}{s} was unchanged at **{amount(a, m)}** in both {name_a} and {name_b}."
    verb = "rose" if change > 0 else "fell"
    pct_text = f" ({pct:+.1f}%)" if pct is not None else ""
    return (f"{noun}{s} {verb} by **{amount(abs(change), m)}{pct_text}**, "
            f"from {fmt(a, m)} in {name_a} to {fmt(b, m)} in {name_b}.")


def rows_answer(columns: list[str], rows: list[dict]) -> str:
    """Generic answer for AI-generated SQL results, whose shape isn't known in advance."""
    if len(rows) == 1 and len(columns) == 1:
        return f"The answer is **{fmt(rows[0][columns[0]])}**."
    if len(rows) == 1:
        lines = "\n".join(f"- **{pretty_column(c)}**: {fmt(rows[0][c])}" for c in columns)
        return f"Here's what I found:\n\n{lines}"
    if len(rows) <= 10 and len(columns) == 1:
        lines = "\n".join(f"- {fmt(r[columns[0]])}" for r in rows)
        return f"I found {len(rows)} matching results:\n\n{lines}"
    if len(rows) <= 10 and len(columns) <= 4:
        lines = "\n".join(
            "- " + ", ".join(f"{pretty_column(c)}: {fmt(r[c])}" for c in columns) for r in rows
        )
        return f"I found {len(rows)} matching rows:\n\n{lines}"
    return f"I found {len(rows)} matching rows; they're shown in the table below."
