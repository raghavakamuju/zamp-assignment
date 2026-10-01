"""Deterministic edge-case tests using crafted signals -- bypasses live
research (PredictLeads/Tavily) so results don't depend on what's currently in
the news. Exercises the judgment/dedup logic directly against app/clients/gemini.py
and app/pipeline.py. Requires GEMINI_API_KEY in .env.

Usage: python scripts/test_edge_cases.py
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.clients import gemini
from app.pipeline import _dedup

TODAY = datetime.now(timezone.utc).date()


def days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


async def case_no_signal():
    """Edge case 1: no usable signal -- signals list is empty."""
    result = await gemini.judge_signals("Jane Doe", "Zzyzx Widgets Cooperative", "VP Ops", [])
    ok = result["confidence"] == "weak" and result["chosen_signal_id"] is None
    print(f"[1] no signal found          -> confidence={result['confidence']!r} chosen={result['chosen_signal_id']!r}  {'PASS' if ok else 'FAIL'}")
    print(f"     reasoning: {result['reasoning']}")
    return ok


async def case_stale_signal():
    """Edge case 2: only signal is >90 days old -- should be treated as stale."""
    signals = [
        {
            "id": "s1",
            "source": "predictleads",
            "type": "news:hires",
            "title": "Acme Corp hires new VP of Operations",
            "snippet": "Acme Corp announced the hire of a new VP of Operations to lead its scaling efforts.",
            "date": days_ago(150),
            "url": "https://example.com/acme-vp-hire",
        }
    ]
    result = await gemini.judge_signals("John Smith", "Acme Corp", "COO", signals)
    ok = result["confidence"] == "weak"
    print(f"[2] stale signal (150d old)  -> confidence={result['confidence']!r} chosen={result['chosen_signal_id']!r}  {'PASS' if ok else 'FAIL'}")
    print(f"     reasoning: {result['reasoning']}")
    return ok


async def case_irrelevant_signal():
    """Edge case 3: only signal has no ops/efficiency angle -- should be rejected."""
    signals = [
        {
            "id": "s1",
            "source": "predictleads",
            "type": "news:receives_award",
            "title": "Acme Corp wins 'Best Office Coffee' award",
            "snippet": "Acme Corp's break room was recognized for having the best coffee in the district.",
            "date": days_ago(5),
            "url": "https://example.com/acme-coffee-award",
        }
    ]
    result = await gemini.judge_signals("John Smith", "Acme Corp", "COO", signals)
    ok = result["confidence"] == "weak"
    print(f"[3] irrelevant signal        -> confidence={result['confidence']!r} chosen={result['chosen_signal_id']!r}  {'PASS' if ok else 'FAIL'}")
    print(f"     reasoning: {result['reasoning']}")
    return ok


def case_duplicate_signals():
    """Edge case 4: two sources reporting the same funding round should merge."""
    signals = [
        {
            "id": "s1",
            "source": "predictleads",
            "type": "financing_event",
            "title": "Raised $40 million (Series B)",
            "snippet": "Series B of $40 million on 2026-08-01.",
            "date": "2026-08-01",
            "url": "https://predictleads-source.example.com/acme-series-b",
        },
        {
            "id": "s2",
            "source": "tavily",
            "type": "web_news",
            "title": "Acme Corp raises $40 million Series B round",
            "snippet": "Acme Corp announced a $40 million Series B funding round led by Example Ventures.",
            "date": "2026-08-03",
            "url": "https://technews.example.com/acme-series-b-announcement",
        },
        {
            "id": "s3",
            "source": "predictleads",
            "type": "job_opening",
            "title": "Hiring: Senior Data Analyst",
            "snippet": "Open role: Senior Data Analyst",
            "date": "2026-08-10",
            "url": "https://acme.example.com/careers/data-analyst",
        },
    ]
    merged, meta = _dedup(signals)
    ok = meta["count_before"] == 3 and meta["count_after"] == 2
    print(f"[4] duplicate signals        -> {meta['count_before']} raw -> {meta['count_after']} merged  {'PASS' if ok else 'FAIL'}")
    for m in merged:
        tag = " (merged, also_reported_by present)" if m.get("also_reported_by") else ""
        print(f"     kept: [{m['id']}] {m['title']}{tag}")
    return ok


async def case_happy_path():
    """Sanity check: a clearly strong, fresh, relevant signal should produce a
    grounded, ready-to-review draft."""
    signals = [
        {
            "id": "s1",
            "source": "predictleads",
            "type": "job_opening",
            "title": "Hiring: Accounts Payable Specialist",
            "snippet": "Acme Corp posted 12 new finance/ops roles this month as it scales its back office.",
            "date": days_ago(10),
            "url": "https://acme.example.com/careers/ap-specialist",
        }
    ]
    judgment = await gemini.judge_signals("John Smith", "Acme Corp", "COO", signals)
    if judgment["confidence"] != "strong":
        print(f"[5] happy path                -> judgment did not reach 'strong' (got {judgment['confidence']!r})  FAIL")
        return False
    chosen = signals[0]
    draft = await gemini.draft_message("John Smith", "Acme Corp", "COO", {**judgment, "signal": chosen})
    grounding = await gemini.check_grounding(draft["body"], [chosen])
    ok = grounding["all_supported"]
    print(f"[5] happy path                -> draft grounded={grounding['all_supported']}  {'PASS' if ok else 'FAIL'}")
    print(f"     subject: {draft['subject']}")
    print(f"     body: {draft['body']}")
    return ok


async def main():
    results = []
    results.append(await case_no_signal())
    results.append(await case_stale_signal())
    results.append(await case_irrelevant_signal())
    results.append(case_duplicate_signals())
    results.append(await case_happy_path())
    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
