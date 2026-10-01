"""Thin async client for PredictLeads v3 (company job openings, news, financing).

Docs: https://docs.predictleads.com/api_endpoints
Auth: X-Api-Key / X-Api-Token headers. Lookups are keyed by domain, not name --
callers must resolve a company name to a domain first (see clients/tavily.py).
"""
import os

import httpx

BASE_URL = "https://predictleads.com/api/v3"


def _headers() -> dict:
    return {
        "X-Api-Key": os.environ["PREDICTLEADS_API_KEY"],
        "X-Api-Token": os.environ["PREDICTLEADS_API_TOKEN"],
    }


async def _get(path: str, params: dict | None = None) -> dict | None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{BASE_URL}{path}", headers=_headers(), params=params or {})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def get_job_openings(domain: str) -> list[dict]:
    data = await _get(f"/companies/{domain}/job_openings", {"active_only": "true", "with_description_only": "true"})
    return data["data"] if data else []


async def get_news_events(domain: str) -> list[dict]:
    data = await _get(f"/companies/{domain}/news_events")
    return data["data"] if data else []


async def get_financing_events(domain: str) -> list[dict]:
    data = await _get(f"/companies/{domain}/financing_events")
    return data["data"] if data else []


async def get_company(domain: str) -> dict | None:
    data = await _get(f"/companies/{domain}")
    return data["data"][0] if data and data.get("data") else None
