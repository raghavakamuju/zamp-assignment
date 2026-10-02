# Code Walkthrough — Personalized Outreach Pipeline

This document explains the codebase file by file, function by function, and
traces the full lifecycle of a request so you can explain any part of it in
the interview. For the product-level pitch (what it does, why these tools),
see [README.md](README.md). This document is about *how the code works*.

## 1. Project map

```
app/
  main.py              FastAPI app: routes, auth endpoints, admin/approval
                        endpoints, SSE streaming
  auth.py              Password hashing, session cookies, admin-access check
  db.py                SQLite persistence (runs, users, sessions tables)
  models.py            Pydantic request schemas
  email_sender.py      Sends the approved draft via Gmail SMTP (the one
                        place in the whole system that actually sends)
  pipeline.py           The 8-stage pipeline orchestration + signal logic
  prompts.py           System prompts for the 3 Gemini calls
  clients/
    gemini.py          Gemini structured-output calls (judge/draft/check)
    predictleads.py    PredictLeads v3 API client (job/news/financing data)
    tavily.py          Tavily search client (domain resolution + fallback)
static/
  index.html, app.js, styles.css   Main dashboard + live run view
  login.html, signup.html, auth.js  Auth pages
  admin.html, admin.js             Shared approval queue: stats, list,
                                    preview pane with approve/reject
runs.db                SQLite database file (created on first run)
scripts/
  test_edge_cases.py       Deterministic tests with crafted signals
  run_test_prospects.py    Batch-submits real test prospects
```

## 2. Full request lifecycle (the story of one run)

1. **Browser loads `/`.** `main.py`'s `index()` route checks for a valid
   session cookie. No cookie (or an invalid one) → `RedirectResponse` to
   `/login`. Valid cookie → serves `static/index.html`.
2. **You submit the form.** `app.js`'s form handler does
   `POST /runs {prospect_name, company_name, title}`.
3. **`create_run()` in `main.py`** inserts a new row into the `runs` table
   via `db.create_run()` (status `pending`), then fires
   `asyncio.create_task(run_pipeline(run_id))` — this runs **in the
   background**, so the HTTP response returns immediately with `{id}`
   rather than blocking for the ~10-30 seconds the pipeline takes.
4. **`app.js` immediately opens an SSE connection** to
   `GET /runs/{id}/stream` via `new EventSource(...)`.
5. **Meanwhile, `run_pipeline()` in `pipeline.py` executes 6 stages in
   order** (research → dedup → judgment → draft → guardrail → done),
   writing to the `runs` row after every single stage via
   `db.start_stage()` / `db.finish_stage()` / `db.update_fields()`.
6. **The SSE endpoint (`stream_run()` in `main.py`) polls the DB every
   500ms** and pushes a new `data: {...}` event to the browser whenever the
   row's JSON representation changes. It stops once `status` reaches any
   value in `TERMINAL_STATUSES` — the 4 pipeline outcomes
   (`ready_for_review`, `flagged_no_signal`, `flagged_ungrounded`, `error`)
   **plus** the 3 post-approval outcomes (`pending_approval`, `sent`,
   `send_failed`) — so the live connection doesn't hang open waiting for
   something the pipeline itself will never produce again.
7. **`app.js`'s `onmessage` handler calls `renderDetail(run)`** every time a
   new event arrives, fully re-drawing the stage checklist, the retrieved
   signals, and (once available) the draft — plus, once `ready_for_review`,
   a **"Request approval to send"** button (only if a `prospect_email` was
   given at intake).
8. If you refresh the page or click away and back, nothing is lost — step
   4 just re-opens with whatever the DB currently has, because `GET
   /runs/{id}` and the SSE stream both read from the same `runs` row that's
   being updated live. **The database is the single source of truth**; the
   frontend never holds state the backend doesn't also have.
9. **Clicking "Request approval"** (`requestApproval()` in `app.js`) posts to
   `POST /runs/{id}/request-approval`, which flips status to
   `pending_approval` — only reachable from `ready_for_review`, and only if
   a `prospect_email` is on file (see §3b for what happens from here).

## 3. Authentication system

**Why it exists**: added on request, not part of the original case-study
scope — the case study's own bar was just "produces a draft ready for human
review," no accounts required. It's intentionally minimal (no password reset
flow, no rate limiting) — the bar is "don't store plaintext passwords and
do scope each account's data correctly," not "survive a security audit."

**Data model** (`db.py`):
```sql
users:    id, email (unique), password_hash, created_at
sessions: token (PK), user_id, created_at
```

**Password hashing** (`auth.py`): PBKDF2-HMAC-SHA256 with a random 16-byte
salt per user, 200,000 iterations — a standard, dependency-free way to avoid
ever storing a plaintext password. `hash_password()` returns `"{salt_hex}:
{digest_hex}"`; `verify_password()` re-derives the digest from the same salt
and compares with `hmac.compare_digest` (constant-time, avoids timing
attacks on the comparison itself).

**Sessions, not JWTs**: `create_session()` generates a random opaque token
(`uuid4().hex`) and stores `{token, user_id}` in the `sessions` table. The
token is set as an `httponly` cookie (`SESSION_COOKIE = "session_token"`),
so client-side JS can never read it (mitigates XSS token theft). Every
authenticated request looks the token up via a JOIN
(`get_user_by_session()`) — simpler to reason about than JWT expiry/refresh
logic for this scope, at the cost of a DB hit per request (irrelevant at
this scale).

**`require_auth()` dependency** (`auth.py`): a FastAPI `Depends()` function
that reads the cookie, looks up the session, and raises `HTTPException(401)`
if invalid. Every `/runs*` endpoint in `main.py` takes
`user: dict = Depends(require_auth)` — FastAPI runs this before the route
body, so an unauthenticated request never reaches the actual handler logic.

**Frontend** (`login.html`, `signup.html`, `auth.js`): plain forms, no
framework. `auth.js`'s `submitAuth()` POSTs to `/auth/login` or
`/auth/signup`; on success the browser now has the cookie and redirects to
`/`. `app.js`'s `loadRuns()` also checks for a `401` response and
redirects to `/login` — a defensive second layer, in case a session
expires mid-use.

**Admin access is a separate, configurable check on top of plain auth**:
`auth.admin_dashboard_restricted()` reads `ADMIN_DASHBOARD_ACCESS` from the
environment (`"everyone"` by default, or `"admin_only"`), and
`require_admin_dashboard()` only consults the `ADMIN_EMAILS` allowlist when
that flag says to. This went through a real design iteration: it started as
a hard-coded `ADMIN_EMAILS`-only allowlist (`require_admin`), got removed
entirely when that account's password was forgotten, then came back as this
toggle so either mode is available without touching code — default open,
restrictable with one `.env` line.

## 3b. Admin approval & send workflow

**Why it exists**: added on request, extending "draft, never send" into
"draft, then two separate humans must explicitly agree before it sends."
Not part of the original case-study scope — the case study's bar was
"produces a draft ready for human review," full stop.

**The request side** (`POST /runs/{id}/request-approval` in `main.py`):
requires the run to be `ready_for_review` *and* to have a `prospect_email`
on file (added to `NewRunRequest` and the `runs` table alongside the
original `prospect_name`) — there's nothing to send to otherwise. Flips
status to `pending_approval`.

**The shared queue** (`db.list_admin_queue()`): deliberately **not** scoped
by `user_id` — it's a JOIN across `runs` and `users` returning every run
with status in `(pending_approval, sent, rejected, send_failed)`, newest
first, with the requester's email attached as `requested_by`. This is what
makes it a *shared* review queue rather than personal history: anyone with
admin access sees every account's requests, not just their own.

**Approve** (`POST /admin/runs/{id}/approve`): calls
`email_sender.send_email()` via `asyncio.to_thread()` (since `smtplib` is
blocking I/O — running it directly in the async route would stall the whole
event loop for every other request during the SMTP round-trip). Success →
`status = "sent"`. Failure (bad credentials, SMTP error, etc.) → the
exception text is stored in `send_error` and `status = "send_failed"`,
surfaced in the UI rather than silently disappearing.

**Reject** (`POST /admin/runs/{id}/reject`): requires a non-empty `reason`
in the request body (`RejectRequest` in `models.py`) — enforced both
client-side (the Reject button alerts if the textarea is empty) and
server-side (`400` if `reason.strip()` is empty), since a rejection with no
explanation defeats the point of having a reviewer at all.

**The stats row** (`admin.js`'s `renderStats()`): computed entirely
client-side from whatever `GET /admin/queue` already returned — no separate
counting endpoint. Counts `pending_approval`/`sent`/`rejected`/`send_failed`
out of the same array that renders the list, so the numbers and the rows
they describe can never drift out of sync with each other.

## 4. The pipeline, stage by stage (`pipeline.py`)

The two helper functions at the top are worth understanding first:

```python
async def _stage(run_id, name, coro):
    # Runs coro, stores whatever it returns as the stage's "output" field,
    # marks the stage done/error. Used for judgment/draft/guardrail, which
    # return a single dict each.

async def _stage_tuple(run_id, name, coro):
    # For coroutines returning (value, display_meta) -- e.g. _research()
    # returns (signals_list, {domain, used_fallback, ...}). Only meta is
    # stored as the stage's displayed output; value is returned to the
    # caller to persist separately. This split exists because storing the
    # raw tuple made the frontend read output.domain as undefined -- a
    # JSON-serialized Python tuple becomes a 2-element array, not an
    # object with those keys. This was a real bug caught during testing.
```

**Stage 1 — Research (`_research`)**:
1. `tavily.resolve_domain(company_name)` — turns "Perplexity AI" into
   `perplexity.ai` (see §5 for how, and where it can fail).
2. If a domain resolved, call PredictLeads' three endpoints in sequence:
   `get_job_openings`, `get_news_events`, `get_financing_events`. Each
   call is wrapped in its own `try/except` — **one dataset failing
   doesn't sink the whole stage**, but the exception text is collected
   into `predictleads_errors` so a systemic failure (e.g. the 403 "email
   not confirmed" error we hit earlier) is visible in the UI instead of
   silently looking like "no data."
3. Each dataset has its own normalizer (`_normalize_job_openings`,
   `_normalize_news_events`, `_normalize_financing_events`) that maps
   PredictLeads' field names onto one common shape:
   `{source, type, title, snippet, date, url}`.
4. If PredictLeads produced nothing (no domain, all three calls failed, or
   the company just isn't in their index), **fall back to
   `tavily.fallback_search()`** — a general web search, normalized to the
   same shape with `source: "tavily"`.
5. Every signal gets a sequential `id` (`s1`, `s2`, ...) — this is the
   handle the judgment/draft/guardrail stages use to refer back to a
   specific signal.

**Stage 2 — Dedup (`_dedup`)**: groups signals that are probably the same
underlying event — same ~21-day date window **and** ≥2 shared keywords in
the title (`_keywords()` extracts 4+ letter words, lowercased). Within a
group, keeps the signal with the longest snippet and attaches the others'
URLs as `also_reported_by`. This is what stops a funding round reported by
both PredictLeads and Tavily from being cited twice or contradicting
itself in the draft.

**Stage 3 — Judgment (`gemini.judge_signals`)**: the first Gemini call. Sends
all deduped signals plus `today`'s date (added after we found the model
was otherwise guessing the current date for staleness checks — see §6) and
gets back a structured `Judgment`: `{confidence, chosen_signal_id,
reasoning, rejected_reason}`. `pipeline.py` then checks:
```python
if judgment["confidence"] != "strong" or not judgment.get("chosen_signal_id"):
    # status -> flagged_no_signal, pipeline stops here
```
This is the single decision point that implements 3 of the 4 edge cases
(no signal / stale signal / irrelevant signal) — they all just produce
`confidence: "weak"` from the LLM, and the code doesn't need to special-case
them individually.

**Stage 4 — Draft (`gemini.draft_message`)**: only reached if confidence was
`strong`. Takes the one chosen signal and writes a complete email, returning
`{subject, body, cited_signal_ids}` — the prompt requires an explicit 3-part
structure (greeting with the prospect's first name only, a 3-5 sentence body
paragraph, a mandatory "Best regards, / Team Zamp" sign-off), each part on
its own line with real blank lines between them. This needed a second pass:
the first version of the prompt produced a greeting but ran it into the body
paragraph with no line break and dropped the sign-off entirely — the current
wording explicitly numbers the 3 parts and says the sign-off "must always be
the last two lines" to make it actually reliable across repeated calls.

**Stage 5 — Guardrail (`gemini.check_grounding`)**: a *second, independent*
Gemini call — it never sees the judgment reasoning, only the draft's body
text and the signal(s) it claims to cite. It extracts every factual claim
and checks each one against the source text, returning
`{claims: [{claim, supported, signal_id}], all_supported}`.
```python
db.set_status(run_id, "ready_for_review" if grounding["all_supported"] else "flagged_ungrounded")
```
This is the 4th edge case, and the one that's actually fired in practice —
twice, independently, on two different kinds of mistakes: once a numeric
conflation ("headcount more than doubling" from a source that only said "job
postings up 108%"), and once an entity mix-up (research for "Apex Labs"
pulled a signal actually describing the unrelated "Apex Systems," and the
draft attributed that company's statistic to the wrong one — the guardrail
caught it the same way, by strictly checking whether the source text
supports the claim, without needing to understand *why* it didn't).

**Error handling**: the whole thing is wrapped in one `try/except` that sets
`status = "error"` on any uncaught exception, so a run never gets stuck in
an intermediate state forever — the live view will show exactly which
stage's icon turned ❌ and the `error` string from that stage.

## 5. The three external API clients

**`predictleads.py`**: thin wrapper, one function per endpoint. All auth is
two static headers (`X-Api-Key`, `X-Api-Token`) read from environment
variables. `_get()` centralizes the request — treats `404` as "no data"
(returns `None`), lets any other non-2xx raise via `raise_for_status()` so
callers see real errors (this is what surfaced the 403 "email not
confirmed" issue instead of it looking like empty data).

**`tavily.py`** does two distinct jobs:
- `resolve_domain()`: searches `"{company} official website"`, excluding a
  blocklist of aggregator domains (`_NON_OFFICIAL_DOMAINS` — Wikipedia,
  LinkedIn, app stores, etc.) **at the Tavily query level** via
  `exclude_domains`, then additionally requires the result's domain to
  contain a token of the company name (`_domain_matches_company()`) before
  accepting it. Returns `None` rather than a wrong domain if nothing
  qualifies — a wrong domain would silently pull PredictLeads data for an
  unrelated company, which is worse than falling back to general search.
- `fallback_search()`: a general "funding OR hiring OR launch OR news"
  search, used either when domain resolution fails or when PredictLeads
  returns nothing for a resolved domain.

**`gemini.py`**: all three LLM calls go through one shared
`_structured_call()` that uses Gemini's `response_schema` parameter (a
Pydantic model) to force valid JSON output, with a retry loop
(`MAX_RETRIES = 3`, exponential backoff) around `ServerError` (transient
503 overload on the free tier). `judge_signals()` additionally
normalizes the `confidence` field defensively: even though the Pydantic
schema declares `Literal["strong", "weak"]`, if the SDK's parsing fails
(`response.parsed is None`) the code falls back to raw unvalidated JSON —
so anything that isn't exactly `"strong"` gets coerced to `"weak"`. This
is a deliberate safe default: when in doubt, decline to draft rather than
force one.

## 6. Data model (`runs.db`, table `runs`)

| Column | Written by | Read by |
|---|---|---|
| `user_id` | `create_run()`, from the authenticated requester | `get_run`/`list_runs` scoping — never null-checked against in the admin queue, which is intentionally unscoped |
| `prospect_email` | intake form | `request-approval` (requires it present), `email_sender.send_email()`'s `to_email` |
| `stages` | `db.start_stage`/`finish_stage` after every pipeline step | live view's expandable stage rows |
| `signals` | after research, again after dedup | "Signals retrieved" section + judgment input |
| `chosen_hook` | after judgment | shows which signal was picked and why |
| `draft_subject`/`draft_body` | after draft | the draft box, and the actual email sent verbatim on approval |
| `confidence` | after judgment | dashboard badge, routing decision |
| `grounding_result` | after guardrail | per-claim ✅/⚠️ breakdown |
| `send_error` | `admin_approve_and_send()`, only on SMTP failure | admin preview pane's "Send failed: ..." message |
| `rejection_reason` | `admin_reject()`, required non-empty | admin preview pane's "Rejected: ..." message |

Two more tables exist alongside `runs`: `users` (`id`, `email`, `password_hash`,
`created_at`) and `sessions` (`token`, `user_id`, `created_at`) — see §3.

Every column that isn't a plain string is stored as a JSON **string**
(`json.dumps`) and parsed back into Python/JS objects on read
(`_row_to_dict()` in `db.py`). This is why `app.js` can just do
`JSON.parse(event.data)` on the SSE payload and get real nested objects.

## 7. Frontend rendering logic (`app.js`)

The one subtlety worth understanding: **the entire detail panel is
re-rendered from scratch on every SSE message** (`container.innerHTML =
...` in `renderDetail()`). This is simple but has a consequence: any DOM
state (like which stage row is expanded) gets wiped on every re-render. We
hit this as a real bug — expanding "Relevance judgment" would
auto-collapse itself a few seconds later while the pipeline was still
running.

**The fix**: `openStages` is a `Set` of stage names that lives *outside*
the render function, at module scope. `renderDetail()` reads from it to
decide whether to add the `"open"` class when building the HTML string,
and the click handler (re-attached after every render, since the old DOM
nodes were just destroyed) mutates the `Set` rather than just toggling a
CSS class directly. State now lives in JS, not in the DOM, so it survives
being redrawn. `selectRun()` resets the set when you switch to a
*different* run, so a fresh run doesn't inherit another run's expanded
rows.

## 8. Design decisions worth being able to defend in the interview

- **Why SSE, not WebSockets**: simpler (`EventSource`, no handshake/
  reconnect logic to hand-roll), and a client that reconnects mid-run can
  always reconstruct full state from `GET /runs/{id}` — nothing is lost on
  a refresh, which isn't automatically true for a stateful WebSocket
  session.
- **Why the guardrail is a *second*, independent LLM call** rather than
  asking the drafting call to "double check itself": a model is a poor
  judge of its own output in the same context; a fresh call that only sees
  the draft text and the source (not the reasoning that produced the
  draft) is a meaningfully more independent check.
- **Why confidence defaults to "weak" on any ambiguity**: every "unsure"
  path in this codebase (dedup's keyword/date thresholds, the Gemini
  confidence normalization, domain resolution returning `None` rather than
  guessing) resolves toward *not drafting* rather than *drafting anyway*.
  That's a deliberate, consistent product stance, not an accident — it
  mirrors Zamp's own stated thesis of escalating rather than guessing.
- **Why signals carry an explicit `id`, and why drafts must cite one**:
  this is what makes grounding checkable at all. Without a stable
  per-signal handle, "does this claim trace back to a real source" would
  have no mechanism to verify against.
- **Why sending requires two separate human actions, not one**: the
  requester clicking "request approval" is not the same action as an admin
  clicking "approve & send" — intentionally. A single-click send conflates
  "I think this is good" with "I'm authorizing this to leave the building,"
  which is exactly the kind of collapsed responsibility that lets a bad
  email slip out because everyone assumed someone else was checking.
- **Why reject requires a typed reason, not just a click**: a bare rejection
  with no reason is a dead end for the requester — they can't learn from it
  or know whether to try a different hook. Enforcing a reason (client- and
  server-side) turns every rejection into feedback instead of a silent no.
- **Why admin access defaults to "everyone logged in" rather than an
  allowlist**: this flipped twice during the build — allowlist first, then
  opened to everyone after the allowlisted account's password was
  forgotten, then made configurable (`ADMIN_DASHBOARD_ACCESS`) so either
  posture is available without a code change. The default favors not
  getting locked out of your own demo over strict least-privilege, which is
  the right tradeoff for a single small team, not necessarily for a larger
  one.
