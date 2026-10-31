"""Validation for LLM-generated SQL (the fallback path only).

Defence in depth:
1. This validator: one statement, SELECT only, only the `sales_clean` view,
   no unknown functions, a row LIMIT added if missing.
2. The connection itself is read-only (see db.connect_readonly), so even SQL
   that slipped past this check could not modify data.
"""
from __future__ import annotations

import sqlglot
from sqlglot import errors, exp

ALLOWED_TABLES = {"sales_clean"}

# Statement / clause types that must never appear anywhere in the tree.
_FORBIDDEN_NAMES = (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Merge",
    "Command", "Pragma", "Attach", "Detach", "Transaction", "Commit", "Rollback",
    "Set", "Use", "Into",
)
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

# Functions sqlglot does not recognise natively arrive as exp.Anonymous; only these are allowed.
ALLOWED_ANONYMOUS_FUNCTIONS = {
    "strftime", "date", "julianday", "printf", "format", "ifnull", "nullif",
    "instr", "substr", "total", "group_concat",
}


class UnsafeSQLError(ValueError):
    """The SQL is not a single, read-only SELECT over the allowed view."""


def validate_sql(sql: str, max_rows: int = 200) -> str:
    """Return a safe, normalised version of `sql`, or raise UnsafeSQLError."""
    if not sql or not sql.strip():
        raise UnsafeSQLError("empty query")

    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except errors.ParseError as exc:
        detail = exc.errors[0].get("description") if getattr(exc, "errors", None) else "syntax error"
        raise UnsafeSQLError(f"query could not be parsed ({detail})") from exc

    if len(statements) != 1:
        raise UnsafeSQLError("exactly one statement is allowed")
    tree = statements[0]

    if not isinstance(tree, exp.Select):
        raise UnsafeSQLError(f"only SELECT queries are allowed (got {type(tree).__name__.upper()})")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeSQLError(f"{type(node).__name__.upper()} is not allowed")
        if isinstance(node, exp.Anonymous) and node.name.lower() not in ALLOWED_ANONYMOUS_FUNCTIONS:
            raise UnsafeSQLError(f"function {node.name}() is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:
            raise UnsafeSQLError("table-valued functions are not allowed")
        if table.args.get("db") or table.args.get("catalog"):
            raise UnsafeSQLError("schema-qualified table names are not allowed")
        if name in cte_names:
            continue
        if name not in ALLOWED_TABLES:
            raise UnsafeSQLError(f"table '{table.name}' is not allowed; only sales_clean can be queried")

    if tree.args.get("limit") is None:
        tree = tree.limit(max_rows)

    return tree.sql(dialect="sqlite")
