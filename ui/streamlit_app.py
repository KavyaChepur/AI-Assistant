"""Streamlit front end. Run the API first, then: streamlit run ui/streamlit_app.py"""
from __future__ import annotations

import json
import os

import pandas as pd
import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")

EXAMPLES = [
    "What is the total revenue?",
    "Which product generated the highest revenue?",
    "What is the monthly revenue trend?",
    "Which region generated the highest sales?",
    "How did revenue change between April and May?",
    "Top 3 products by units sold in the South region",
    "What is the profit margin?",
]

st.set_page_config(page_title="Sales BI Assistant", page_icon="📊", layout="centered")


@st.cache_data(ttl=60)
def fetch_schema() -> dict | None:
    try:
        response = requests.get(f"{API_URL}/schema", timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def set_question(text: str) -> None:
    st.session_state.question = text


with st.sidebar:
    st.header("Try a question")
    for example in EXAMPLES:
        st.button(example, on_click=set_question, args=(example,), width="stretch")

    st.header("Data quality")
    schema = fetch_schema()
    if schema is None:
        st.warning(f"API not reachable at {API_URL}")
    else:
        quality = schema["data_quality"]
        st.write(
            f"{quality['total_rows']} rows loaded, {quality['analytics_rows']} used, "
            f"{quality['excluded_rows']} excluded. Coverage: {schema['coverage']}."
        )
        st.caption(f"Status counts (valid rows): {quality['status_counts']}")
        for issue in quality["issues"]:
            st.caption(f"Row {issue['row']} ({issue['transaction_id']}): {issue['reason']}")

st.title("Sales BI assistant")
st.caption("Ask about the sales data in plain English. Every number comes from a SQL query you can inspect.")

question = st.text_input("Your question", key="question", placeholder="e.g. Which region generated the highest sales?")
ask = st.button("Ask", type="primary")

if ask:
    if not question.strip():
        st.warning("Type a question first.")
        st.stop()
    with st.spinner("Working on it..."):
        try:
            response = requests.post(f"{API_URL}/ask", json={"question": question}, timeout=60)
        except requests.RequestException as exc:
            st.error(f"Could not reach the API at {API_URL}: {exc}")
            st.stop()

    if response.status_code != 200:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        if isinstance(detail, list):  # FastAPI validation errors
            detail = "; ".join(d.get("msg", "") for d in detail)
        st.error(f"Error {response.status_code}: {detail}")
        st.stop()

    data = response.json()
    status = data["status"]
    if status == "ok" and data["verified"]:
        st.success(data["answer"])
    elif status == "ok":
        st.warning(data["answer"])
    elif status == "no_data":
        st.info(data["answer"])
    else:
        st.error(data["answer"])
    if data.get("caveat"):
        st.caption(data["caveat"])
    if data.get("answer_source") == "llm":
        st.caption("Worded by Gemini; every number was checked against the query results.")

    summary = data.get("summary") or {}
    if "change" in summary:
        c1, c2, c3 = st.columns(3)
        c1.metric(summary["period_a"], f"{summary['value_a']:,}")
        c2.metric(summary["period_b"], f"{summary['value_b']:,}")
        pct = summary.get("change_pct")
        c3.metric("Change", f"{summary['change']:+,}", f"{pct:+.1f}%" if pct is not None else None)

    if data["rows"]:
        frame = pd.DataFrame(data["rows"], columns=data["columns"] or None)
        st.subheader("Supporting data")
        st.dataframe(frame, hide_index=True, width="stretch")
        chart = data.get("chart")
        if chart and chart["x"] in frame and chart["y"] in frame:
            series = frame.set_index(chart["x"])[chart["y"]]
            if chart["type"] == "line":
                st.line_chart(series)
            else:
                st.bar_chart(series)

    with st.expander("How this was answered"):
        if data["sql"]:
            st.markdown("**SQL executed** (read-only connection)")
            st.code(data["sql"], language="sql")
            if data["params"]:
                st.caption(f"Parameters: {data['params']}")
        st.markdown("**Plan from the LLM**")
        st.code(json.dumps(data["plan"], indent=2), language="json")
        q = data["data_quality"]
        st.caption(f"Based on {q['rows_used']} of {q['rows_loaded']} rows ({q['rows_excluded']} excluded).")
