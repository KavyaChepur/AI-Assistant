"""Runtime settings, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is optional; plain environment variables also work
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    model_name: str
    csv_path: Path
    db_path: Path
    enable_sql_fallback: bool
    narrate_answers: bool
    sql_row_limit: int
    sql_timeout_seconds: float


def get_settings() -> Settings:
    """Read settings at call time so tests can override them with environment variables."""
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        model_name=MODEL_NAME,
        csv_path=Path(os.getenv("CSV_PATH", str(BASE_DIR / "data" / "sales.csv"))),
        db_path=Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "sales.db"))),
        enable_sql_fallback=os.getenv("ENABLE_SQL_FALLBACK", "true").strip().lower() in {"1", "true", "yes"},
        narrate_answers=os.getenv("NARRATE_ANSWERS", "true").strip().lower() in {"1", "true", "yes"},
        sql_row_limit=int(os.getenv("SQL_ROW_LIMIT", "200")),
        sql_timeout_seconds=float(os.getenv("SQL_TIMEOUT_SECONDS", "2")),
    )
