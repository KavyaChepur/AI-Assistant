"""Optional LLM rewrite of the answer into natural language, with a number check.

Flow: the template answer (answers.py) is always built first from the query
results. Gemini may then rephrase it, given only the question, that answer and
the result rows. Every number in Gemini's text must already appear in those
inputs. If it invents, rounds or recalculates any number, the rewrite is
discarded and the template answer is used. So the LLM shapes the wording, and
the database still owns the numbers.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from .config import MODEL_NAME

log = logging.getLogger(__name__)

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

NARRATOR_PROMPT = """\
You rewrite a data answer for a business user in clear, natural English.
You receive the user's question, a correct draft answer, and the data rows behind it.

Rules:
- Answer the question directly in the first sentence.
- Use only facts and numbers that appear in the draft or the data rows, written exactly as they
  appear there (same digits, commas and decimals). Never calculate, round, estimate or add numbers,
  and never write numbers as words or in millions/thousands.
- When there are more than 3 items, use a Markdown list, one item per line: numbered for rankings,
  bullets otherwise.
- Put the key figure in **bold**. Keep it under about 100 words.
- No headings, no greetings, no preamble such as "Sure" or "Based on the data".
- Do not explain causes, give advice, or speculate beyond the data.
"""


class Narrator(Protocol):
    def narrate(self, question: str, draft: str, rows: list[dict], summary: dict) -> str: ...


def _numbers_in(text: str) -> set[float]:
    found = set()
    for token in NUMBER.findall(text):
        try:
            found.add(round(float(token.replace(",", "")), 2))
        except ValueError:
            continue
    return found


def allowed_numbers(question: str, draft: str, rows: list[dict], summary: dict) -> set[float]:
    """Every number the rewrite may use: those in the question, the draft and the data."""
    blob = " ".join([question, draft, json.dumps(rows, default=str), json.dumps(summary, default=str)])
    allowed = _numbers_in(blob)
    allowed |= {float(i) for i in range(1, len(rows) + 1)}  # list numbering: "1.", "2.", ...
    return allowed


def unsupported_numbers(text: str, allowed: set[float]) -> list[str]:
    """Numbers in `text` that are not in `allowed` (empty list = the text passes)."""
    return [t for t in NUMBER.findall(text) if round(float(t.replace(",", "")), 2) not in allowed]


def polish(result, question: str, narrator: Narrator | None):
    """Replace result.answer with the narrator's text only if it passes the number check."""
    if narrator is None or result.status != "ok":
        return result
    try:
        text = (narrator.narrate(question, result.answer, result.rows, result.summary) or "").strip()
    except Exception:  # the rewrite is optional: any failure keeps the template answer
        log.warning("Narrator failed; keeping the template answer", exc_info=True)
        return result
    if not text:
        return result
    bad = unsupported_numbers(text, allowed_numbers(question, result.answer, result.rows, result.summary))
    if bad:
        log.warning("Narration rejected, numbers not in the data: %s", bad)
        return result
    result.answer = text
    result.answer_source = "llm"
    return result


class GeminiNarrator:
    def __init__(self, api_key: str, model_name: str = MODEL_NAME):
        from google import genai
        from google.genai import types

        self.model_name = model_name
        self._client = genai.Client(api_key=api_key)
        self._config = types.GenerateContentConfig(system_instruction=NARRATOR_PROMPT, temperature=0)

    def narrate(self, question: str, draft: str, rows: list[dict], summary: dict) -> str:
        prompt = (
            f"Question: {question}\n\n"
            f"Draft answer (correct; use its numbers exactly):\n{draft}\n\n"
            f"Data rows (JSON):\n{json.dumps(rows[:30], default=str)}"
        )
        response = self._client.models.generate_content(model=self.model_name, contents=prompt, config=self._config)
        return response.text or ""
