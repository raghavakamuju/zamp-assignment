"""The 8-stage PS-3 pipeline: intake -> research -> normalize -> dedup ->
judgment -> draft -> guardrail -> persist. Every stage writes its result to
the run row as it completes (see app/db.py) so the live view and dashboard
always read from the same source of truth.
"""
import re
from datetime import datetime, timezone

from app import db
from app.clients import gemini, predictleads, tavily

STALE_SIGNAL_DAYS = 90


async def _stage(run_id: str, name: str, coro):
    db.start_stage(run_id, name)
    try:
        result = await coro
        db.finish_stage(run_id, name, "done", output=result)
        return result
    except Exception as e:
        db.finish_stage(run_id, name, "error", error=str(e))
        raise


async def _stage_tuple(run_id: str, name: str, coro):
    """Like _stage, but for coroutines returning (value, display_meta) --
    stores only display_meta as the stage output (what the live view renders),
    not the whole tuple. Storing the raw tuple made the frontend read
    output.domain/output.used_fallback as undefined, since a JSON-serialized
    Python tuple becomes a 2-element array, not an object with those keys."""
    db.start_stage(run_id, name)
    try:
        value, meta = await coro
        db.finish_stage(run_id, name, "done", output=meta)
        return value
    except Exception as e:
        db.finish_stage(run_id, name, "error", error=str(e))
        raise


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10]


def _normalize_job_openings(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        a = item.get("attributes", {})
        out.append(
            {
                "source": "predictleads",
                "type": "job_opening",
                "title": f"Hiring: {a.get('title', 'unknown role')}",
                "snippet": (a.get("description") or "")[:500] or f"Open role: {a.get('title')}",
                "date": _iso_date(a.get("first_seen_at")),
                "url": a.get("url"),
            }
        )
    return out


def _normalize_news_events(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        a = item.get("attributes", {})
        url = a.get("most_relevant_source_url") or a.get("source_url")
        if not url and a.get("source_urls"):
            url = a["source_urls"][0]
        out.append(
            {
                "source": "predictleads",
                "type": f"news:{a.get('category', 'event')}",
                "title": a.get("summary") or a.get("category", "News event"),
                "snippet": a.get("article_sentence") or a.get("summary") or "",
                "date": _iso_date(a.get("effective_date") or a.get("found_at")),
                "url": url,
            }
        )
    return out


def _normalize_financing_events(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        a = item.get("attributes", {})
        out.append(
            {
                "source": "predictleads",
                "type": "financing_event",
                "title": f"Raised {a.get('amount', 'funding')} ({a.get('financing_type', 'financing')})",
                "snippet": f"{a.get('financing_type', 'Financing')} of {a.get('amount', 'an undisclosed amount')} "
                f"on {a.get('effective_date', 'an unspecified date')}.",
                "date": _iso_date(a.get("effective_date") or a.get("found_at")),
                "url": (a.get("source_urls") or [None])[0],
            }
        )
    return out


async def _research(company_name: str) -> tuple[list[dict], dict]:
    domain = await tavily.resolve_domain(company_name)
    signals: list[dict] = []
    predictleads_errors: list[str] = []

    if domain:
        for fetcher, normalizer in (
            (predictleads.get_job_openings, _normalize_job_openings),
            (predictleads.get_news_events, _normalize_news_events),
            (predictleads.get_financing_events, _normalize_financing_events),
        ):
            try:
                raw = await fetcher(domain)
                signals.extend(normalizer(raw))
            except Exception as e:
                # One dataset failing shouldn't sink the whole research stage, but
                # swallowing this silently made a real 403 auth failure look like
                # "no data for this company" -- surface it instead so a systemic
                # problem (bad credentials, plan doesn't include API access) is
                # visible in the live view rather than hidden behind the fallback.
                predictleads_errors.append(f"{fetcher.__name__}: {e}")

    used_fallback = False
    if not signals:
        used_fallback = True
        signals = await tavily.fallback_search(company_name)

    for i, s in enumerate(signals):
        s["id"] = f"s{i + 1}"

    return signals, {
        "domain": domain,
        "used_fallback": used_fallback,
        "signal_count": len(signals),
        "predictleads_errors": predictleads_errors,
    }


def _keywords(title: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z]{4,}", title.lower()))


def _same_date_window(d1: str | None, d2: str | None, days: int = 21) -> bool:
    if not d1 or not d2:
        return False
    try:
        dt1, dt2 = datetime.fromisoformat(d1), datetime.fromisoformat(d2)
    except ValueError:
        return False
    return abs((dt1 - dt2).days) <= days


def _dedup(signals: list[dict]) -> tuple[list[dict], dict]:
    used = [False] * len(signals)
    merged = []
    for i, s in enumerate(signals):
        if used[i]:
            continue
        group = [s]
        used[i] = True
        for j in range(i + 1, len(signals)):
            if used[j]:
                continue
            other = signals[j]
            if _same_date_window(s.get("date"), other.get("date")) and len(
                _keywords(s["title"]) & _keywords(other["title"])
            ) >= 2:
                group.append(other)
                used[j] = True
        primary = dict(max(group, key=lambda x: len(x.get("snippet") or "")))
        if len(group) > 1:
            primary["also_reported_by"] = [g["url"] for g in group if g is not primary and g.get("url")]
        merged.append(primary)

    for idx, m in enumerate(merged):
        m["id"] = f"s{idx + 1}"
    return merged, {"count_before": len(signals), "count_after": len(merged)}


async def run_pipeline(run_id: str) -> None:
    run = db.get_run_unscoped(run_id)
    prospect_name, company_name, title = run["prospect_name"], run["company_name"], run["title"]

    try:
        db.set_status(run_id, "researching")
        signals = await _stage_tuple(run_id, "research", _research(company_name))
        db.update_fields(run_id, signals=signals)

        deduped = await _stage_tuple(run_id, "dedup", _async_dedup(signals))
        db.update_fields(run_id, signals=deduped)

        db.set_status(run_id, "judging")
        judgment = await _stage(
            run_id, "judgment", gemini.judge_signals(prospect_name, company_name, title, deduped)
        )
        db.update_fields(run_id, confidence=judgment["confidence"])

        if judgment["confidence"] != "strong" or not judgment.get("chosen_signal_id"):
            db.update_fields(run_id, chosen_hook=judgment)
            db.set_status(run_id, "flagged_no_signal")
            return

        chosen = next((s for s in deduped if s["id"] == judgment["chosen_signal_id"]), None)
        db.update_fields(run_id, chosen_hook={**judgment, "signal": chosen})

        db.set_status(run_id, "drafting")
        draft = await _stage(run_id, "draft", gemini.draft_message(prospect_name, company_name, title, chosen))
        db.update_fields(run_id, draft_subject=draft["subject"], draft_body=draft["body"])

        db.set_status(run_id, "checking")
        cited = [s for s in deduped if s["id"] in draft.get("cited_signal_ids", [])] or [chosen]
        grounding = await _stage(run_id, "guardrail", gemini.check_grounding(draft["body"], cited))
        db.update_fields(run_id, grounding_result=grounding)

        db.set_status(run_id, "ready_for_review" if grounding["all_supported"] else "flagged_ungrounded")
    except Exception:
        db.set_status(run_id, "error")
        raise


async def _async_dedup(signals: list[dict]) -> tuple[list[dict], dict]:
    return _dedup(signals)
