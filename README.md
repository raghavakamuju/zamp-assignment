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
   automatically — the original requester has to explicitly **request approval**
   before anything moves further.
6. **A second human reviews and decides.** Any logged-in account can open the
   admin queue, read the exact draft, and either **approve & send** (a real
   email goes out over Gmail SMTP) or **reject with a typed reason** — nothing
   sends without this explicit second step either.

Every one of those steps is visible in the UI as it happens, every past run is
kept in a history view, and the admin queue keeps its own full history (sent /
rejected / failed), not just what's currently pending.

## 2. Architecture

```
Browser (static/index.html, app.js)
   |  POST /runs  { prospect_name, prospect_email, company_name, title }
   |  session cookie required (app/auth.py) -- sign up / log in first
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
  5. Judgment       -- app/clients/gemini.py picks the single best "hook" with
                        structured reasoning, or explicitly declines if nothing
                        is a good fit
  6. Draft          -- Gemini drafts a complete email (greeting, body, sign-off)
                        around the chosen hook
  7. Guardrail      -- a second Gemini call fact-checks every claim in the draft
                        against the actual retrieved signal text
  8. Persist        -- final status: ready_for_review | flagged_no_signal |
                        flagged_ungrounded | error

   v
Human loop (only reachable from ready_for_review):
  requester clicks "Request approval to send"  -->  status = pending_approval
   v
Admin queue (static/admin.html, app/main.py /admin/*) -- any logged-in account
by default (see ADMIN_DASHBOARD_ACCESS), lists every request across ALL users:
  - Approve & send  -> app/email_sender.py sends via Gmail SMTP -> status = sent
                        (or send_failed, with the error recorded, on failure)
  - Reject + reason -> status = rejected, reason stored and shown back

   v
SQLite (runs.db, app/db.py) -- single source of truth read by both the
   live view (via SSE), the per-user history dashboard (GET /runs), and the
   shared admin queue (GET /admin/queue)
```

**Why these tools:**
- **PredictLeads** (free, 100 credits/month, no card) — the primary signal
  source. One API gives job postings, news events, and financing events, which
  covers "hiring surge / funding round / exec change" in one place.
- **Tavily** (free, 1,000 credits/month, no card) — does double duty: (a)
  resolves a company name to a domain (PredictLeads looks companies up by
  domain, not name), and (b) a fallback general web search when PredictLeads
  has no data for a company (common for very small or pre-launch companies).
- **Gemini** (free tier, no card; exact model set by `MODEL` in
  `app/clients/gemini.py`) — the reasoning/drafting layer. Chosen over a paid
  model so the whole pipeline runs at zero cost. Structured output
  (`response_schema`) is used for all three LLM calls so responses are
  validated JSON, not free text that needs fragile parsing, with retry logic
  around transient free-tier overload (`503`) errors.
- **FastAPI + SQLite + vanilla JS** — no build step, nothing to deploy, fast to
  iterate on in a one-week build. Server-Sent Events (not WebSockets) for the
  live view, because a client can always reconstruct current state from
  `GET /runs/{id}` if it reconnects mid-run — nothing is lost on a refresh.

## 3. Data model

Three SQLite tables (see `app/db.py`): `runs` (the actual work), `users` and
`sessions` (auth — see §Authentication below).

`runs`:

| column | what it holds |
|---|---|
| `user_id` | which account created this run (per-user history scoping) |
| `prospect_name`, `prospect_email`, `company_name`, `title` | the input |
| `status` | current state — `pending`/`researching`/.../`ready_for_review`/`flagged_no_signal`/`flagged_ungrounded`/`error`, then if approval is requested: `pending_approval`/`sent`/`rejected`/`send_failed` |
| `stages` | JSON array, one entry per stage: `{name, status, started_at, finished_at, output, error}` — this is what the live view renders incrementally |
| `signals` | the normalized + deduped research results |
| `chosen_hook` | the judgment step's output (which signal, why, confidence) |
| `draft_subject`, `draft_body` | the generated email |
| `confidence` | `strong` or `weak`, set by the judgment step |
| `grounding_result` | the guardrail step's per-claim fact-check |
| `send_error` | the exception message if an approved send actually failed |
| `rejection_reason` | the admin's typed reason, if rejected |

## 3b. Authentication

Simple session-cookie auth (`app/auth.py`): passwords hashed with PBKDF2 (never
stored plain), a random session token per login, checked on every request via
a FastAPI dependency. Not hardened for production (no password reset flow) —
the bar here is "don't store plaintext passwords," not "survive a security
audit." See `CODE_WALKTHROUGH.md` §3 for the full mechanism.

**Admin queue access** is controlled by `ADMIN_DASHBOARD_ACCESS` in `.env`:
`everyone` (default — any logged-in account can review and approve/reject
anyone's requests) or `admin_only` (restricted to the comma-separated
`ADMIN_EMAILS` allowlist). This can be flipped without touching code.

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

**A 5th safety net has fired in practice, unprompted**, beyond the 4 designed
cases above — the grounding guardrail (stage 7) has independently caught two
different real mistakes during testing: once a numeric conflation ("job
postings up 108%" drafted as "headcount more than doubling"), and once an
entity mix-up (research for "Apex Labs" pulled a signal that was actually
about the unrelated "Apex Systems," and the draft attributed that company's
statistic to the wrong one). Both were blocked before reaching
`ready_for_review`, which is the guardrail doing its job on cases nobody
designed for ahead of time.

## 5. Setup

### Get free API keys (all no-card-required)

1. **PredictLeads** — sign up at predictleads.com, grab your API key + API
   token from *Settings → API Keys* (free: 100 credits/month). **Note:**
   you must confirm your account email before the API will authorize
   requests (a 403 with "email not confirmed" means this step is pending).
2. **Tavily** — sign up at tavily.com (free: 1,000 credits/month).
3. **Gemini** — create a key at [aistudio.google.com](https://aistudio.google.com/apikey)
   (free tier, no card).

### For the admin approve-and-send feature (optional)

4. **Gmail App Password** — in your Google Account → Security → 2-Step
   Verification → App Passwords, generate a 16-character app password for
   "Mail." Use this (not your real Gmail password) as `SMTP_APP_PASSWORD`,
   alongside the Gmail address itself as `SMTP_SENDER_EMAIL`. Without these
   two set, the pipeline (research → draft → guardrail) still works fully —
   only the admin "Approve & send" button will fail.
5. Optionally set `ADMIN_DASHBOARD_ACCESS=admin_only` and `ADMIN_EMAILS=` to
   restrict who can see the admin queue (defaults to `everyone` logged in).

Copy `.env.example` to `.env` and fill in the values.

### Run it

```bash
python3.12 -m venv .venv          # 3.10+ required; this repo was built/tested on 3.12
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. Sign up for an account, submit a prospect +
company (+ their email, if you want to test the approval/send path), watch
the live run view, check the history panel for past runs. Once a run reaches
`ready_for_review`, click "Request approval to send," then open `/admin` to
approve (sends a real email) or reject it.

## 6. Known limitations (deliberate scope cuts for a 1-week build)

- Domain resolution (company name → domain) is a best-effort Tavily search, not
  a verified company database — it can occasionally resolve to the wrong
  domain for companies with generic names, or pull a signal about a different,
  unrelated company that happens to share a word in its name (seen in testing
  with "Apex Labs" vs. the unrelated "Apex Systems") — the grounding guardrail
  is the safety net that catches this downstream, not something prevented
  upstream.
- PredictLeads' free tier (100 credits/month) is the practical ceiling on how
  many fresh research calls this can make per month; Tavily fallback has much
  more headroom (1,000/month).
- Nothing auto-sends on its own — but this isn't the single-human-review model
  it started as: two separate explicit actions are required (the requester
  clicks "request approval," a second account clicks "approve & send"), never
  one. That second step is a deliberate addition beyond the original case-study
  scope.
- The admin queue is a flat "everyone logged in" or "allowlist" toggle
  (`ADMIN_DASHBOARD_ACCESS`), not a real roles/permissions system — fine for a
  small team, not something to scale past that without revisiting.
- SMTP credentials live in `.env` in plaintext (standard for this scope, but
  worth knowing if this were ever deployed somewhere less trusted).

## 7. One-paragraph version for a non-technical audience

*Imagine a researcher who, the moment you give them a name and company, goes and
checks if anything real and interesting has happened there recently — a new
funding round, a hiring push, a notable hire — then writes you a short, honest
email draft built around that one fact, and hands it back saying "here's what I
found, and here's a draft." If they can't find anything worth mentioning, they
tell you that too, instead of making something up. Nothing goes out from here
on its own — the person who asked for the research has to explicitly say "send
this," and then a second person has to actually approve it before it leaves.
That's this tool: it does the 20 minutes of research a busy sales rep doesn't
have time for, shows its work, and still keeps two humans in the loop before
anything reaches a real inbox.*

