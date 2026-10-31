# Mini AI BI Assistant

Ask questions about sales data in plain English and get answers backed by real SQL queries.

Gemini is used twice, and never as the source of a number:

1. It **interprets** the question into a structured plan. Application code builds the SQL from
   that plan, runs it on a read-only SQLite database, and writes a template answer from the results.
2. It **rewords** that answer into natural language. Code then checks every number in the rewrite
   against the query results; if any number is new or changed, the template answer is used instead.

```
Question ─► Gemini (plan) ─► SQL from whitelisted parts ─► SQLite (read-only) ─► template answer
                                                                                   │
                                          Gemini (wording) ─► number check ─► final answer + SQL + rows
```

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then put your key in GEMINI_API_KEY
```

Get a Gemini API key from Google AI Studio. The model is set by one line in `app/config.py`:

```python
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
```

Override it with `GEMINI_MODEL` in `.env` if you want a different model.

## Run

Two terminals:

```bash
uvicorn app.main:app --reload                 # API on http://localhost:8000 (docs at /docs)
streamlit run ui/streamlit_app.py             # UI on http://localhost:8501
```

The API rebuilds `data/sales.db` from `data/sales.csv` on every start.

### API example

```bash
curl -X POST http://localhost:8000/ask -H "Content-Type: application/json" \
     -d '{"question": "Which product generated the highest revenue?"}'
```

```json
{
  "status": "ok",
  "answer": "Monitor has the highest revenue: 473,000.",
  "verified": true,
  "plan": {"kind": "aggregate", "measure": "revenue", "group_by": "product", "sort": "value_desc", "limit": 1},
  "sql": "SELECT product AS label, SUM(revenue) AS value, COUNT(*) AS orders FROM sales_clean GROUP BY product ORDER BY value DESC, label ASC LIMIT 200",
  "rows": [{"label": "Monitor", "value": 473000, "orders": 9, "share_pct": 32.9}, "...all 7 products..."],
  "data_quality": {"rows_loaded": 51, "rows_used": 48, "rows_excluded": 3}
}
```

| Endpoint | Purpose |
|---|---|
| `POST /ask` | Answer a question. `status` is `ok`, `no_data` or `unsupported` |
| `GET /health` | Liveness, model name, whether a key is configured |
| `GET /schema` | Known values per dimension, data coverage, excluded rows and why |

Errors: `422` invalid request (empty, missing or over 500 characters), `502` the LLM failed or
returned an unusable plan, `503` no API key configured, `500` anything unexpected.

## Supported questions

All five example questions, plus combinations of these building blocks:

| Part | Options |
|---|---|
| Measures | revenue, gross revenue, discount, units sold, number of orders, average order value, number of different products |
| Group by | product, category, region, sales channel, month |
| Filters | any of the dimensions above, plus month ranges (Q1, "from Feb to Apr") |
| Shapes | totals, rankings (top/bottom N), breakdowns, monthly trend, change between two months |

Examples: "Top 3 products by units sold in the South region", "Average order value by sales
channel in Q2", "How did online revenue change between March and May?".

Questions outside these shapes can fall back to **AI-generated SQL** (validated, read-only, and
labelled *unverified* in the answer). Turn this off with `ENABLE_SQL_FALLBACK=false`.

Expected answers for the five example questions:

| Question | Answer |
|---|---|
| Total revenue | 1,439,200 (48 orders) |
| Highest-revenue product | Monitor, 473,000 |
| Monthly trend | Jan 210,250 · Feb 234,600 · Mar 223,900 · Apr 240,700 · May 318,500 · Jun 211,250 |
| Highest-sales region | South, 411,200 |
| April to May change | +77,800 (+32.3%) |

## Data handling

- Every CSV row is stored in the `sales` table. Invalid rows get `is_valid = 0` and a
  `reject_reason`, so nothing disappears silently.
- Analytics only read the `sales_clean` view: valid rows with `status = 'Completed'`, with
  `revenue = quantity × unit_price − discount_amount` computed once.
- Dates are parsed as **DD-MM-YYYY** (03-01-2026 is 3 January) and stored as ISO dates.
- Checks: missing fields, unparseable dates, non-numeric or non-positive quantity and price,
  negative discounts, discounts larger than the order, duplicate transaction IDs.

In the supplied file, 3 of 51 rows are excluded:

| Row | Problem | Handling |
|---|---|---|
| TXN0033 | Status is Returned | Stored as valid, excluded from revenue |
| TXN0034 | Quantity is 0 | Flagged invalid |
| TXN0051 | Unit price is blank | Flagged invalid |

## How wrong or unsafe answers are prevented

1. **The LLM never produces numbers.** It returns a plan, and the answer is first written by templates
   from query results. Gemini may reword it, but a rewrite containing any number that isn't in the
   question, the template answer or the result rows is discarded. Turn rewording off with
   `NARRATE_ANSWERS=false`.
2. **Structured output, validated twice.** Gemini is constrained to a JSON schema, and the result is
   validated again with Pydantic (enums for measures and dimensions, month format, limits).
3. **Filters are checked against real data.** A product, region or month that doesn't exist returns a
   clear `no_data` message listing what does exist, instead of a misleading zero.
4. **SQL is built only from whitelisted identifiers**, and filter values are bound parameters, so a plan
   cannot inject SQL.
5. **Generated SQL (fallback only)** must parse as a single `SELECT` on `sales_clean`, with no other tables,
   schema prefixes, PRAGMA or unknown functions. A `LIMIT` is added, it runs with a 2-second timeout, and
   the answer is marked unverified.
6. **The database connection is read-only** (`mode=ro` plus `PRAGMA query_only`), so destructive SQL
   fails at the driver even if everything above were bypassed.
7. **Out-of-scope questions** (profit, customers, forecasts, requests to modify data) are classified as
   `unsupported` and answered with an explanation instead of a guess.

## Tests

```bash
pytest                                   # 80 tests, no API key or network needed
GEMINI_API_KEY=... pytest -m live        # optional: checks the real Gemini planner on 9 questions
```

The LLM is replaced by a fake planner in the main suite, so the tests are deterministic. They cover
the five example questions with exact figures, invalid requests, unsupported and destructive
questions, unknown products and months, ties, messy CSV input, the SQL guard (16 attack cases), and
the read-only connection.

## Project structure

```
app/
  config.py      settings and MODEL_NAME
  db.py          CSV loading, validation, read-only connection, query timeout
  catalog.py     real values per dimension (fed to the prompt and used for validation)
  semantic.py    Plan model, whitelisted measures and dimensions, SQL builder
  planner.py     Gemini prompt, response schema, plan parsing
  sql_guard.py   validator for AI-generated SQL
  engine.py      runs a plan and builds the result
  answers.py     answer templates: sentences, numbered lists, bullets
  narrator.py    optional Gemini rewording + number check
  main.py        FastAPI app
ui/streamlit_app.py
tests/
data/sales.csv
ARCHITECTURE.md  AI_USAGE.md
```

## Known limitations

- One table and one year of data. A month name without a year is read as the latest year in the data.
- "Compare" handles two single months, not quarter against quarter (the SQL fallback can).
- One measure per question; ratios between groups (such as online share per region) need the SQL fallback.
- Fallback SQL answers depend on the LLM writing correct SQL. They are labelled unverified for that reason.
- The discount is assumed to be per order line, not per unit. The currency is not stated in the data,
  so amounts are shown without a symbol.
- The LLM can misread a question. The plan and SQL are shown with every answer so this can be spotted.
- The number check on reworded answers catches invented or changed figures, but not a wrong direction
  word (for example "rose" instead of "fell") when the numbers themselves are correct.
- Rewording adds a second Gemini call, roughly a second per question.
- No authentication or rate limiting. The data is reloaded on startup rather than ingested incrementally.
