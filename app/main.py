"""HTTP API.

POST /ask     question -> plan (Gemini) -> SQL (semantic layer) -> answer + supporting data
GET  /health  liveness + model name
GET  /schema  what the data contains and which rows were excluded
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager, closing

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .catalog import load_catalog
from .config import MODEL_NAME, get_settings
from .db import connect_readonly, load_csv
from .engine import QueryEngine
from .narrator import GeminiNarrator, Narrator, polish
from .planner import GeminiPlanner, Planner, PlannerError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bi_assistant")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    report = load_csv(settings.csv_path, settings.db_path)
    log.info(
        "Loaded %s rows: %s used for analytics, %s excluded",
        report.total_rows, report.analytics_rows, report.excluded_rows,
    )
    with closing(connect_readonly(settings.db_path)) as conn:
        catalog = load_catalog(conn)

    app.state.settings = settings
    app.state.report = report
    app.state.catalog = catalog
    app.state.engine = QueryEngine(
        catalog,
        sql_enabled=settings.enable_sql_fallback,
        row_cap=settings.sql_row_limit,
        timeout_seconds=settings.sql_timeout_seconds,
    )
    app.state.planner = None  # created on first use, so the API starts even without a key
    app.state.narrator = None
    yield


app = FastAPI(title="Mini AI BI Assistant", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


class AskRequest(BaseModel):
    question: str = Field(..., max_length=500, examples=["Which product generated the highest revenue?"])

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("question must not be empty")
        return value


class AskResponse(BaseModel):
    question: str
    status: str  # ok | no_data | unsupported
    answer: str  # Markdown
    verified: bool
    caveat: str | None
    answer_source: str  # "template" or "llm"
    plan: dict | None
    sql: str | None
    params: list
    columns: list[str]
    rows: list[dict]
    summary: dict
    chart: dict | None
    data_quality: dict


def get_planner(request: Request) -> Planner:
    state = request.app.state
    if state.planner is None:
        if not state.settings.gemini_api_key:
            raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not set on the server.")
        state.planner = GeminiPlanner(
            api_key=state.settings.gemini_api_key,
            catalog=state.catalog,
            sql_enabled=state.settings.enable_sql_fallback,
            model_name=state.settings.model_name,
        )
    return state.planner


def get_narrator(request: Request) -> Narrator | None:
    """Optional: rewrites answers in natural language. None = use the templates as they are."""
    state = request.app.state
    if not (state.settings.narrate_answers and state.settings.gemini_api_key):
        return None
    if state.narrator is None:
        state.narrator = GeminiNarrator(api_key=state.settings.gemini_api_key, model_name=state.settings.model_name)
    return state.narrator


@app.post("/ask", response_model=AskResponse)
def ask(
    body: AskRequest,
    request: Request,
    planner: Planner = Depends(get_planner),
    narrator: Narrator | None = Depends(get_narrator),
) -> AskResponse:
    state = request.app.state
    try:
        plan = planner.plan(body.question)
    except PlannerError as exc:
        raise HTTPException(status_code=502, detail=f"Could not interpret the question: {exc}") from exc

    with closing(connect_readonly(state.settings.db_path)) as conn:
        result = state.engine.run(plan, conn)
    result = polish(result, body.question, narrator)

    log.info("Q=%r kind=%s status=%s source=%s sql=%s",
             body.question, plan.kind, result.status, result.answer_source, result.sql)
    report = state.report
    return AskResponse(
        question=body.question,
        plan=plan.model_dump(exclude_none=True),
        data_quality={
            "rows_loaded": report.total_rows,
            "rows_used": report.analytics_rows,
            "rows_excluded": report.excluded_rows,
        },
        **result.to_dict(),
    )


@app.get("/health")
def health(request: Request) -> dict:
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "llm_configured": bool(request.app.state.settings.gemini_api_key),
        "sql_fallback": request.app.state.settings.enable_sql_fallback,
        "narrate_answers": request.app.state.settings.narrate_answers,
    }


@app.get("/schema")
def schema(request: Request) -> dict:
    return {
        "table": "sales_clean",
        "data_quality": request.app.state.report.to_dict(),
        **request.app.state.catalog.to_dict(),
    }


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})
