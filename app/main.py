import asyncio
import json
from pathlib import Path

import truststore

truststore.inject_into_ssl()  # use the OS trust store -- needed on networks with a TLS-inspecting proxy

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app import auth, db, email_sender
from app.auth import SESSION_COOKIE, require_admin_dashboard, require_auth
from app.models import AuthRequest, NewRunRequest, RejectRequest
from app.pipeline import run_pipeline

TERMINAL_STATUSES = {
    "ready_for_review",
    "flagged_no_signal",
    "flagged_ungrounded",
    "error",
    "pending_approval",
    "sent",
    "send_failed",
}
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Outreach Pipeline")


@app.on_event("startup")
async def startup() -> None:
    db.init_db()


# --- auth ---------------------------------------------------------------


@app.post("/auth/signup")
async def signup(body: AuthRequest, response: Response):
    if db.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="an account with that email already exists")
    user_id = db.create_user(body.email, auth.hash_password(body.password))
    token = db.create_session(user_id)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/auth/login")
async def login(body: AuthRequest, response: Response):
    user = db.get_user_by_email(body.email)
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = db.create_session(user["id"])
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# --- pages ----------------------------------------------------------------


@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/signup")
async def signup_page():
    return FileResponse(STATIC_DIR / "signup.html")


@app.get("/")
async def index(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not db.get_user_by_session(token):
        return RedirectResponse(url="/login")
    return FileResponse(STATIC_DIR / "index.html")


# --- runs API (all require an authenticated session) ----------------------


@app.post("/runs")
async def create_run(body: NewRunRequest, user: dict = Depends(require_auth)):
    run_id = db.create_run(user["id"], body.prospect_name, body.prospect_email, body.company_name, body.title)
    asyncio.create_task(run_pipeline(run_id))
    return {"id": run_id}


@app.get("/runs")
async def list_runs(user: dict = Depends(require_auth)):
    return db.list_runs(user["id"])


@app.get("/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(require_auth)):
    run = db.get_run(run_id, user["id"])
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.post("/runs/{run_id}/request-approval")
async def request_approval(run_id: str, user: dict = Depends(require_auth)):
    run = db.get_run(run_id, user["id"])
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run["status"] != "ready_for_review":
        raise HTTPException(status_code=409, detail=f"run is '{run['status']}', not ready_for_review")
    if not run.get("prospect_email"):
        raise HTTPException(status_code=400, detail="this run has no prospect email on file -- can't request approval to send")
    db.set_status(run_id, "pending_approval")
    return {"ok": True}


@app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, user: dict = Depends(require_auth)):
    async def event_source():
        last_payload = None
        while True:
            run = db.get_run(run_id, user["id"])
            if not run:
                yield "event: error\ndata: {}\n\n"
                return
            payload = json.dumps(run)
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if run["status"] in TERMINAL_STATUSES:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(event_source(), media_type="text/event-stream")


# --- admin: review + send approved drafts -----------------------------------
# Access controlled by ADMIN_DASHBOARD_ACCESS env var: 'everyone' (default) or
# 'admin_only' (gated by the ADMIN_EMAILS allowlist). See app/auth.py.


@app.get("/admin")
async def admin_page(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    user = db.get_user_by_session(token) if token else None
    if not user:
        return RedirectResponse(url="/login")
    if auth.admin_dashboard_restricted() and user["email"].lower() not in auth.admin_emails():
        raise HTTPException(status_code=403, detail="admin access required")
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/admin/queue")
async def admin_queue(user: dict = Depends(require_admin_dashboard)):
    return db.list_admin_queue()


@app.post("/admin/runs/{run_id}/approve")
async def admin_approve_and_send(run_id: str, user: dict = Depends(require_admin_dashboard)):
    run = db.get_run_unscoped(run_id)  # any user reviewing the shared approval queue, not just the requester
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail=f"run is '{run['status']}', not pending_approval")
    try:
        await asyncio.to_thread(
            email_sender.send_email, run["prospect_email"], run["draft_subject"], run["draft_body"]
        )
    except Exception as e:
        db.update_fields(run_id, send_error=str(e))
        db.set_status(run_id, "send_failed")
        raise HTTPException(status_code=502, detail=f"send failed: {e}")
    db.set_status(run_id, "sent")
    return {"ok": True}


@app.post("/admin/runs/{run_id}/reject")
async def admin_reject(run_id: str, body: RejectRequest, user: dict = Depends(require_admin_dashboard)):
    run = db.get_run_unscoped(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail=f"run is '{run['status']}', not pending_approval")
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="a rejection reason is required")
    db.update_fields(run_id, rejection_reason=body.reason.strip())
    db.set_status(run_id, "rejected")
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
