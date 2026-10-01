"""Tavily web search client -- used for (1) resolving a company name to a
domain PredictLeads can look up, and (2) as a fallback signal source when
PredictLeads has nothing for a company.

Docs: https://docs.tavily.com/documentation/api-reference/endpoint/search
"""
import os
import re
from urllib.parse import urlparse

import httpx

BASE_URL = "https://api.tavily.com/search"

# Aggregator/social/app-store/review sites that routinely outrank a company's
# actual homepage for "<company> official website" queries -- a blocklist alone
# is whack-a-mole (there's always another app store or review site), so this is
# a secondary filter; the primary filter is the name-match check below.
_NON_OFFICIAL_DOMAINS = {
    "wikipedia.org", "linkedin.com", "crunchbase.com", "bloomberg.com",
    "twitter.com", "x.com", "facebook.com", "forbes.com", "techcrunch.com",
    "prnewswire.com", "businesswire.com", "glassdoor.com", "indeed.com",
    "medium.com", "reveliolabs.com", "owler.com", "apps.apple.com",
    "play.google.com", "g2.com", "capterra.com", "producthunt.com",
    "trustpilot.com", "instagram.com", "youtube.com", "reddit.com",
}

_STOPWORDS = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "group", "holdings"}


def _registrable_domain(url: str) -> str | None:
    """netloc minus a leading www., e.g. 'www.perplexity.ai' -> 'perplexity.ai'.
    Not full public-suffix-list aware, but good enough for the checks below."""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _is_official_candidate(domain: str) -> bool:
    return not any(domain == d or domain.endswith(f".{d}") for d in _NON_OFFICIAL_DOMAINS)


def _company_tokens(company_name: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", company_name.lower())
    return [t for t in cleaned.split() if t and t not in _STOPWORDS]


def _domain_matches_company(domain: str, company_name: str) -> bool:
    """Require the domain to actually reference the company -- a blocklist can
    only reject known-bad domains, it can't recognize every unrelated site
    (app stores, review sites, etc.) that might rank highly. This is what
    catches those: 'apps.apple.com' doesn't contain any token of the company
    name, so it's rejected even though it wasn't blocklisted."""
    tokens = _company_tokens(company_name)
    if not tokens:
        return True  # nothing to match against; don't block on an empty name
    first_label = domain.split(".")[0]
    return any(tok in first_label or first_label in tok for tok in tokens if len(tok) >= 3)


async def _search(query: str, max_results: int = 5, topic: str = "general", exclude_domains: list[str] | None = None) -> dict:
    body = {"query": query, "max_results": max_results, "topic": topic, "search_depth": "basic"}
    if exclude_domains:
        body["exclude_domains"] = exclude_domains
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            BASE_URL,
            headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
            json=body,
        )
    resp.raise_for_status()
    return resp.json()


async def resolve_domain(company_name: str) -> str | None:
    """Best-effort: find the official domain for a company name. Requires the
    domain to contain a token of the company name AND not be a known
    aggregator/app-store/social site -- returning None (falls through to
    fallback_search) is safer than returning a wrong domain, which could
    silently pull PredictLeads data for an unrelated company.

    Excludes known aggregator domains at the Tavily query level (not just in
    post-filtering) -- otherwise a top-5 result page that's entirely Wikipedia/
    app-store/directory listings pushes the real homepage out of range and
    resolution fails even though the site definitely exists and ranks well
    once those competitors are removed from contention."""
    result = await _search(
        f"{company_name} official website",
        max_results=8,
        exclude_domains=list(_NON_OFFICIAL_DOMAINS),
    )
    for item in result.get("results", []):
        domain = _registrable_domain(item["url"])
        if domain and _is_official_candidate(domain) and _domain_matches_company(domain, company_name):
            return domain
    return None


async def fallback_search(company_name: str) -> list[dict]:
    """General web search for recent signal when PredictLeads has nothing."""
    result = await _search(f"{company_name} funding OR hiring OR launch OR news 2026", max_results=5, topic="news")
    return [
        {
            "source": "tavily",
            "type": "web_news",
            "title": item["title"],
            "snippet": item["content"][:500],
            "date": item.get("published_date"),
            "url": item["url"],
        }
        for item in result.get("results", [])
    ]
