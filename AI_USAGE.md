# AI usage

> Draft: rewrite this in your own words and add anything specific to how you worked.
> Interviewers will ask about it, so it should describe what you actually did.

## AI tools used

- **Claude** (claude.ai): architecture discussion, code generation, tests and documentation.
- **Gemini 2.5 Flash** (`gemini-2.5-flash`): the runtime LLM inside the app, which turns questions into query plans.

## How AI was used during development

1. **Design.** Discussed the trade-off between free text-to-SQL and whitelisted queries. Chose a hybrid:
   a small semantic layer (whitelisted measures and dimensions) for most questions, and validated
   text-to-SQL as a labelled fallback.
2. **Code generation.** Generated the modules, then reviewed each one: the SQL builder, the SQL guard
   rules, and the answer templates.
3. **Test design.** Calculated the expected figures for the example questions independently with
   pandas, and used them as exact assertions in the tests.
4. **Review.** Checked every generated SQL path for injection and write access. Ran the guard against
   a list of attack queries.

## Problems encountered and how they were debugged

| Problem | How it was found | Fix |
|---|---|---|
| Dates are DD-MM-YYYY; a default parser would read 03-01-2026 as 1 March | Inspected the raw CSV before loading | Parse with an explicit `%d-%m-%Y` format; test that TXN0001 is 3 January |
| Returned order, zero quantity and blank price rows would inflate or break totals | Profiled the data before building queries | Flag rows instead of dropping them; filter in one `sales_clean` view |
| The first design (five fixed functions) couldn't answer simple variations | Tried rephrased and combined questions | Replaced it with a query-spec semantic layer covering hundreds of question shapes |
| `pragma_table_info('sales')` parsed as a table with an empty name, not a function | Ran the SQL guard against attack queries | Reject table-valued functions and unknown functions explicitly |
| `this is not sql` parsed as an expression rather than raising a parse error | A failing test | Still rejected (not a SELECT); corrected the test's expectation |
| Template answers read mechanically ("Revenue by month: Jan 2026 210,250; ...") | User testing | Rewrote templates as sentences and lists, and added a Gemini rewording step whose numbers are checked against the query results |
| An invented product name would return 0 instead of an error | Edge-case testing | Validate filter values against the real catalog; return `no_data` with the known values |

## What I would improve with another day

- Send validation errors back to the LLM for one automatic retry before failing.
- Multi-step agent mode for "why" questions (chain several plans).
- Follow-up questions with conversation memory ("and for the North region?").
- An evaluation set of about 50 questions, tracking planner accuracy per model.
- Docker Compose, authentication, rate limiting and request tracing.
- Postgres and incremental ingestion for larger datasets.
