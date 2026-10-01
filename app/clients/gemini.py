"""Gemini calls for relevance judgment, drafting, and the grounding/guardrail check.

Uses Gemini's structured-output mode (response_schema + response_mime_type=application/json)
so every call returns validated JSON instead of free text that needs fragile parsing.
Gemini 2.5 Flash has a genuinely free tier (no card required) -- see
https://ai.google.dev/gemini-api/docs/rate-limits
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Literal

from google import genai
from google.genai import types
from google.genai.errors import ServerError
from pydantic import BaseModel

from app import prompts

MODEL = "gemini-3.1-flash-lite"

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


class Judgment(BaseModel):
    confidence: Literal["strong", "weak"]
    chosen_signal_id: str | None
    reasoning: str
    rejected_reason: str | None


class Draft(BaseModel):
    subject: str
    body: str
    cited_signal_ids: list[str]


class Claim(BaseModel):
    claim: str
    supported: bool
    signal_id: str


class GroundingCheck(BaseModel):
    claims: list[Claim]
    all_supported: bool


MAX_RETRIES = 3


async def _structured_call(system: str, user_content: str, schema: type[BaseModel]) -> dict:
    for attempt in range(MAX_RETRIES):
        try:
            response = await _client.aio.models.generate_content(
                model=MODEL,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            break
        except ServerError:
            # Transient overload (503) on the free tier -- retry with backoff rather
            # than letting one flaky call sink an otherwise-good run, per the demo-
            # safety requirement (must run live and unassisted in the interview).
            if attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(2**attempt)
    if response.parsed is not None:
        return response.parsed.model_dump()
    return json.loads(response.text)


async def judge_signals(prospect_name: str, company_name: str, title: str | None, signals: list[dict]) -> dict:
    user_content = json.dumps(
        {
            "prospect_name": prospect_name,
            "company_name": company_name,
            "title": title,
            "today": datetime.now(timezone.utc).date().isoformat(),
            "signals": signals,
        }
    )
    result = await _structured_call(prompts.JUDGE_SYSTEM, user_content, Judgment)
    # Belt-and-suspenders: the schema constrains this to "strong"/"weak", but when
    # response.parsed is None we fall back to unvalidated raw JSON (see
    # _structured_call), so a model that drifts to a synonym like "high" or "low"
    # can still slip through. Default anything non-exact to "weak" -- the safe
    # direction is to decline drafting, never to force one on an ambiguous read.
    if result.get("confidence") != "strong":
        result["confidence"] = "weak"
    return result


async def draft_message(prospect_name: str, company_name: str, title: str | None, hook: dict) -> dict:
    user_content = json.dumps({"prospect_name": prospect_name, "company_name": company_name, "title": title, "hook": hook})
    return await _structured_call(prompts.DRAFT_SYSTEM, user_content, Draft)


async def check_grounding(draft_body: str, cited_signals: list[dict]) -> dict:
    user_content = json.dumps({"draft_body": draft_body, "source_signals": cited_signals})
    return await _structured_call(prompts.GROUNDING_SYSTEM, user_content, GroundingCheck)
