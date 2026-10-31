"""Data layer: load the sales CSV into SQLite and open read-only connections.

Design choice: every CSV row is stored, including bad ones. Bad rows get
is_valid = 0 and a human-readable reject_reason, so the app can show exactly
what was excluded. Analytics only ever read the `sales_clean` view.
"""
from __future__ import annotations

import csv
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.request import pathname2url

DATE_FORMAT = "%d-%m-%Y"  # the CSV uses DD-MM-YYYY (03-01-2026 is 3 January)

EXPECTED_COLUMNS = (
    "Transaction_Id",
    "Order_date",
    "Product",
    "Category",
    "Region",
    "Sales_channel",
    "Quantity",
    "Unit_price",
    "Discount_amount",
    "Status",
)

SCHEMA_SQL = """
DROP VIEW IF EXISTS sales_clean;
DROP TABLE IF EXISTS sales;

CREATE TABLE sales (
    row_num          INTEGER PRIMARY KEY,  -- line number in the CSV (header = line 1)
    transaction_id   TEXT,
    order_date       TEXT,                 -- ISO YYYY-MM-DD, NULL if unparseable
    raw_order_date   TEXT,                 -- the original text, kept for auditing
    product          TEXT,
    category         TEXT,
    region           TEXT,
    sales_channel    TEXT,
    quantity         INTEGER,
    unit_price       REAL,
    discount_amount  REAL,
    status           TEXT,
    is_valid         INTEGER NOT NULL,     -- 1 = usable for analytics
    reject_reason    TEXT                  -- why is_valid = 0
);

CREATE INDEX idx_sales_valid_status ON sales (is_valid, status);

-- The only object analytics may query: valid, completed orders with revenue computed once.
CREATE VIEW sales_clean AS
SELECT
    transaction_id,
    order_date,
    strftime('%Y-%m', order_date)            AS month,
    product,
    category,
    region,
    sales_channel,
    quantity,
    unit_price,
    discount_amount,
    quantity * unit_price - discount_amount  AS revenue
FROM sales
WHERE is_valid = 1 AND status = 'Completed';
"""


class DataLoadError(RuntimeError):
    """The CSV could not be loaded at all (missing file, wrong columns)."""


class QueryTimeout(RuntimeError):
    """A query ran longer than the allowed time."""


@dataclass
class LoadReport:
    csv_path: str
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    analytics_rows: int = 0  # rows in sales_clean (valid AND Completed)
    status_counts: dict[str, int] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)  # one entry per invalid row

    @property
    def excluded_rows(self) -> int:
        return self.total_rows - self.analytics_rows

    def to_dict(self) -> dict:
        return {
            "csv_path": self.csv_path,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "analytics_rows": self.analytics_rows,
            "excluded_rows": self.excluded_rows,
            "status_counts": self.status_counts,
            "issues": self.issues,
        }


def _text(raw: dict, column: str, problems: list[str]) -> str | None:
    value = " ".join((raw.get(column) or "").split())  # trim and collapse inner whitespace
    if not value:
        problems.append(f"missing {column}")
        return None
    return value


def _number(
    raw: dict,
    column: str,
    problems: list[str],
    *,
    integer: bool = False,
    blank_default: float | None = None,
) -> float | int | None:
    text = (raw.get(column) or "").strip().replace(",", "")
    if not text:
        if blank_default is not None:
            return blank_default
        problems.append(f"missing {column}")
        return None
    try:
        value = float(text)
    except ValueError:
        problems.append(f"non-numeric {column} '{text}'")
        return None
    if not math.isfinite(value):
        problems.append(f"non-numeric {column} '{text}'")
        return None
    if integer:
        if not value.is_integer():
            problems.append(f"non-integer {column} '{text}'")
            return None
        return int(value)
    return value


def clean_row(raw: dict, seen_ids: set[str]) -> tuple[dict, list[str]]:
    """Normalise one CSV row and list everything wrong with it (empty list = valid)."""
    problems: list[str] = []

    transaction_id = _text(raw, "Transaction_Id", problems)
    if transaction_id:
        if transaction_id in seen_ids:
            problems.append(f"duplicate Transaction_Id {transaction_id}")
        seen_ids.add(transaction_id)

    raw_date = _text(raw, "Order_date", problems)
    order_date = None
    if raw_date:
        try:
            order_date = datetime.strptime(raw_date, DATE_FORMAT).date().isoformat()
        except ValueError:
            problems.append(f"invalid Order_date '{raw_date}' (expected DD-MM-YYYY)")

    product = _text(raw, "Product", problems)
    category = _text(raw, "Category", problems)
    region = _text(raw, "Region", problems)
    sales_channel = _text(raw, "Sales_channel", problems)
    status = _text(raw, "Status", problems)
    if status:
        status = status.title()  # "completed" / "COMPLETED" -> "Completed"

    quantity = _number(raw, "Quantity", problems, integer=True)
    if quantity is not None and quantity <= 0:
        problems.append(f"Quantity must be greater than 0 (got {quantity})")

    unit_price = _number(raw, "Unit_price", problems)
    if unit_price is not None and unit_price <= 0:
        problems.append(f"Unit_price must be greater than 0 (got {unit_price:g})")

    # A blank discount means "no discount"; anything else must be a valid amount.
    discount = _number(raw, "Discount_amount", problems, blank_default=0.0)
    if discount is not None:
        if discount < 0:
            problems.append(f"Discount_amount cannot be negative (got {discount:g})")
        elif quantity and unit_price and quantity > 0 and unit_price > 0 and discount > quantity * unit_price:
            problems.append("Discount_amount is larger than the order value")

    row = {
        "transaction_id": transaction_id,
        "order_date": order_date,
        "raw_order_date": raw_date,
        "product": product,
        "category": category,
        "region": region,
        "sales_channel": sales_channel,
        "quantity": quantity,
        "unit_price": unit_price,
        "discount_amount": discount,
        "status": status,
    }
    return row, problems


def load_csv(csv_path: Path | str, db_path: Path | str) -> LoadReport:
    """(Re)build the SQLite database from the CSV and return a data-quality report."""
    csv_path, db_path = Path(csv_path), Path(db_path)
    if not csv_path.exists():
        raise DataLoadError(f"CSV file not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in EXPECTED_COLUMNS if c not in header]
        if missing:
            raise DataLoadError(f"CSV is missing columns: {', '.join(missing)}")
        reader.fieldnames = header
        raw_rows = list(reader)

    report = LoadReport(csv_path=str(csv_path))
    seen_ids: set[str] = set()
    records = []
    for line_number, raw in enumerate(raw_rows, start=2):
        row, problems = clean_row(raw, seen_ids)
        is_valid = not problems
        reason = "; ".join(problems) or None
        records.append({**row, "row_num": line_number, "is_valid": int(is_valid), "reject_reason": reason})

        report.total_rows += 1
        if is_valid:
            report.valid_rows += 1
            report.status_counts[row["status"]] = report.status_counts.get(row["status"], 0) + 1
        else:
            report.invalid_rows += 1
            report.issues.append(
                {"row": line_number, "transaction_id": row["transaction_id"], "reason": reason}
            )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            """INSERT INTO sales (row_num, transaction_id, order_date, raw_order_date, product, category,
                                  region, sales_channel, quantity, unit_price, discount_amount, status,
                                  is_valid, reject_reason)
               VALUES (:row_num, :transaction_id, :order_date, :raw_order_date, :product, :category,
                       :region, :sales_channel, :quantity, :unit_price, :discount_amount, :status,
                       :is_valid, :reject_reason)""",
            records,
        )
        conn.commit()
        report.analytics_rows = conn.execute("SELECT COUNT(*) FROM sales_clean").fetchone()[0]
    finally:
        conn.close()
    return report


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open the database read-only. Writes fail at the driver level, whatever SQL arrives."""
    uri = f"file:{pathname2url(str(Path(db_path).resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_query(
    conn: sqlite3.Connection, sql: str, params: list | tuple = (), timeout_seconds: float = 2.0
) -> tuple[list[str], list[dict]]:
    """Run a SELECT with a time limit and return (column names, rows as dicts)."""
    deadline = time.monotonic() + timeout_seconds
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cursor = conn.execute(sql, list(params))
        rows = cursor.fetchall()
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise QueryTimeout(f"query took longer than {timeout_seconds:g}s") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
    columns = [d[0] for d in cursor.description or []]
    return columns, [dict(zip(columns, tuple(r))) for r in rows]
