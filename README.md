# Personalized Outreach Pipeline (PS-3)

A working answer to: *"Sales reps can't spend 20 minutes researching each of 200
prospects, so outreach is generic and gets ignored. Build something that researches
a prospect for real, finds one genuine signal worth mentioning, and drafts a
personalized message grounded in that fact — for a human to review before sending."*

## 1. What it does, in plain terms

You type in a prospect's name and company. The system then:

1. **Looks the company up for real** — recent funding, hiring activity, and news,
   pulled from live data sources (not guessed by an LLM).
2. **Judges which fact is actually worth mentioning** — not every fact is a good
   hook. A sports sponsorship doesn't justify an ops-automation pitch; a hiring
   surge does. If nothing qualifies, it says so instead of forcing a weak angle.
3. **Drafts a short outreach email** built around that one fact.
4. **Fact-checks its own draft** — a second pass verifies every claim in the email
   is actually backed by the retrieved data, not invented by the LLM.
5. **Stops.** The draft sits in a "pending review" state. Nothing is ever sent
   automatically — a human always makes the final call.

Every one of those five steps is visible in the UI as it happens, and every past
run is kept in a history view.

## 2. Architecture

```
Browser (static/index.html, app.js)
   |  POST /runs  { prospect_name, company_name, title }
   v
FastAPI (app/main.py)
   |  kicks off app/pipeline.py as a background asyncio task
   |  browser opens GET /runs/{id}/stream (Server-Sent Events) to watch it live
   v
Pipeline (app/pipeline.py) -- 8 stages, each one persisted to SQLite as it finishes:

  1. Intake        -- store the request, status = pending
  2. Research       -- app/clients/tavily.py resolves company name -> domain,
                        then app/clients/predictleads.py pulls job openings,
                        news events, and financing events for that domain.
                        If PredictLeads has nothing, Tavily's general web search
                        is used as a fallback.
  3. Normalize      -- every result (regardless of source) becomes the same
                        shape: {id, source, type, title, snippet, date, url}
  4. Dedup          -- signals describing the same underlying event (e.g. the
                        same funding round reported by two sources) are merged
                        before judgment sees them
  5. Judgment       -- app/clients/gemini.py (Gemini 2.5 Flash) picks the single
                        best "hook" with structured reasoning, or explicitly
                        declines if nothing is a good fit
  6. Draft          -- Gemini drafts a short email around the chosen hook
  7. Guardrail      -- a second Gemini call fact-checks every claim in the draft
                        against the actual retrieved signal text
  8. Persist        -- final status: ready_for_review | flagged_no_signal |
                        flagged_ungrounded | error

   v
SQLite (runs.db, app/db.py) -- single source of truth read by both the
   live view (via SSE) and the history dashboard (via GET /runs)
```

**Why these tools:**
- **PredictLeads** (free, 100 credits/month, no card) — the primary signal
  source. One API gives job postings, news events, and financing events, which
  covers "hiring surge / funding round / exec change" in one place.
- **Tavily** (free, 1,000 credits/month, no card) — does double duty: (a)
  resolves a company name to a domain (PredictLeads looks companies up by
  domain, not name), and (b) a fallback general web search when PredictLeads
  has no data for a company (common for very small or pre-launch companies).
- **Gemini 2.5 Flash** (free tier, no card) — the reasoning/drafting layer.
  Chosen over a paid model so the whole pipeline runs at zero cost. Structured
  output (`response_schema`) is used for all three LLM calls so responses are
  validated JSON, not free text that needs fragile parsing.
- **FastAPI + SQLite + vanilla JS** — no build step, nothing to deploy, fast to
  iterate on in a one-week build. Server-Sent Events (not WebSockets) for the
  live view, because a client can always reconstruct current state from
  `GET /runs/{id}` if it reconnects mid-run — nothing is lost on a refresh.

## 3. Data model

Everything lives in one SQLite table, `runs` (see `app/db.py`):

| column | what it holds |
|---|---|
| `prospect_name`, `company_name`, `title` | the input |
| `status` | current pipeline state (see list above) |
| `stages` | JSON array, one entry per stage: `{name, status, started_at, finished_at, output, error}` — this is what the live view renders incrementally |
| `signals` | the normalized + deduped research results |
| `chosen_hook` | the judgment step's output (which signal, why, confidence) |
| `draft_subject`, `draft_body` | the generated email |
| `confidence` | `strong` or `weak`, set by the judgment step |
| `grounding_result` | the guardrail step's per-claim fact-check |

## 4. Edge cases handled deliberately

1. **No usable signal found** — PredictLeads and Tavily both come up empty or
   irrelevant → the judgment step returns `confidence: weak`, no draft is
   generated, status becomes `flagged_no_signal`. The system explains what it
   searched and why nothing qualified, rather than forcing a generic email.
2. **Stale signal** — the only available signal is old (the judgment prompt is
   instructed to reject anything >90 days stale if nothing fresher exists) —
   e.g. an executive hire that may have since left. Downgraded to
   `confidence: weak` rather than drafting on outdated information.
3. **Irrelevant signal for this pitch** — e.g. the only signal is an award or
   sponsorship with no plausible connection to an operations/efficiency pain
   point. The judgment prompt explicitly instructs rejecting a forced
   connection rather than writing a strained hook.
4. **Duplicate signals across sources** — e.g. a financing round appears in
   both a PredictLeads news event and a Tavily search result. The dedup stage
   merges same-event signals (same date window + keyword overlap) before
   judgment ever sees them, so the draft never double-cites or contradicts
   itself on one underlying fact.

## 5. Setup

### Get free API keys (all no-card-required)

1. **PredictLeads** — sign up at predictleads.com, grab your API key + API
   token from *Settings → API Keys* (free: 100 credits/month).
2. **Tavily** — sign up at tavily.com (free: 1,000 credits/month).
3. **Gemini** — create a key at [aistudio.google.com](https://aistudio.google.com/apikey)
   (free tier, no card, Gemini 2.5 Flash).

Copy `.env.example` to `.env` and fill in the four values.

### Run it

```bash
python3.12 -m venv .venv          # 3.10+ required; this repo was built/tested on 3.12
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. Submit a prospect + company, watch the live run
view, check the history panel for past runs.

## 6. Known limitations (deliberate scope cuts for a 1-week build)

- Domain resolution (company name → domain) is a best-effort Tavily search, not
  a verified company database — it can occasionally resolve to the wrong
  domain for companies with generic names.
- PredictLeads' free tier (100 credits/month) is the practical ceiling on how
  many fresh research calls this can make per month; Tavily fallback has much
  more headroom (1,000/month).
- No auth/multi-user support — this is a single-operator tool, matching the
  scope of the case study.
- Nothing ever auto-sends. That's not a limitation to fix — it's the point:
  the system's job ends at "here's a grounded draft," a human decides the rest.

## 7. One-paragraph version for a non-technical audience

*Imagine a researcher who, the moment you give them a name and company, goes and
checks if anything real and interesting has happened there recently — a new
funding round, a hiring push, a notable hire — then writes you a short, honest
email draft built around that one fact, and hands it back saying "here's what I
found, and here's a draft — you decide if it's worth sending." If they can't find
anything worth mentioning, they tell you that too, instead of making something up.
That's this tool. It never sends anything itself; it just does the 20 minutes of
research a busy sales rep doesn't have time for, and shows its work.*
# zamp-assignment-
# zamp-assignment
