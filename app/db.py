"""SQLite persistence for pipeline runs.

Each call opens a short-lived connection -- fine at case-study scale and keeps
the module free of shared connection/locking state.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "runs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    prospect_name TEXT NOT NULL,
    prospect_email TEXT,
    company_name TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stages TEXT NOT NULL DEFAULT '[]',
    signals TEXT NOT NULL DEFAULT '[]',
    chosen_hook TEXT,
    draft_subject TEXT,
    draft_body TEXT,
    confidence TEXT,
    grounding_result TEXT,
    send_error TEXT,
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)
        # Migrations for DBs created before these columns existed (CREATE TABLE
        # IF NOT EXISTS above is a no-op on an already-existing table).
        cols = [row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN user_id TEXT")
        if "prospect_email" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN prospect_email TEXT")
        if "send_error" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN send_error TEXT")
        if "rejection_reason" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN rejection_reason TEXT")


def create_run(
    user_id: str, prospect_name: str, prospect_email: str | None, company_name: str, title: str | None
) -> str:
    run_id = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO runs (id, user_id, prospect_name, prospect_email, company_name, title,
               status, created_at, updated_at, stages, signals)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, '[]', '[]')""",
            (run_id, user_id, prospect_name, prospect_email, company_name, title, now, now),
        )
    return run_id


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("stages", "signals", "chosen_hook", "grounding_result"):
        if d.get(field):
            d[field] = json.loads(d[field])
        elif field in ("stages", "signals"):
            d[field] = []
    return d


def get_run(run_id: str, user_id: str) -> dict | None:
    # Scoped by user_id too, not just run_id -- otherwise anyone who learns/
    # guesses another user's run UUID (e.g. from a shared link or browser
    # history) could view their draft and research data directly. Use this
    # for every user-facing API call. For the pipeline's own internal
    # read/write of a run it already owns, use get_run_unscoped instead.
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_run_unscoped(run_id: str) -> dict | None:
    """For trusted internal callers only (the pipeline updating its own run) --
    never expose this via an API route, since it skips the user_id check."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_runs(user_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, prospect_name, company_name, status, confidence,
               created_at, updated_at FROM runs WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


ADMIN_QUEUE_STATUSES = ("pending_approval", "sent", "rejected", "send_failed")


def list_admin_queue() -> list[dict]:
    """Every run that has ever entered the approval flow -- pending, sent,
    rejected, or failed to send -- across ALL users (intentionally not scoped
    by user_id, since this is a shared review queue, not personal history).
    Most recent first, so new requests surface at the top."""
    placeholders = ",".join("?" for _ in ADMIN_QUEUE_STATUSES)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT runs.id, runs.prospect_name, runs.prospect_email, runs.company_name,
               runs.title, runs.draft_subject, runs.draft_body, runs.status,
               runs.created_at, runs.updated_at, runs.send_error, runs.rejection_reason,
               users.email AS requested_by
               FROM runs JOIN users ON users.id = runs.user_id
               WHERE runs.status IN ({placeholders}) ORDER BY runs.created_at DESC""",
            ADMIN_QUEUE_STATUSES,
        ).fetchall()
    return [dict(r) for r in rows]


def set_status(run_id: str, status: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), run_id),
        )


def update_fields(run_id: str, **fields) -> None:
    """Update arbitrary columns; dict/list values are JSON-encoded."""
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    vals.append(_now())
    vals.append(run_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE runs SET {', '.join(cols)}, updated_at = ? WHERE id = ?",
            vals,
        )


def create_user(email: str, password_hash: str) -> str:
    user_id = str(uuid.uuid4())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email.lower(), password_hash, _now()),
        )
    return user_id


def get_user_by_email(email: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
    return dict(row) if row else None


def create_session(user_id: str) -> str:
    token = uuid.uuid4().hex
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, _now()),
        )
    return token


def get_user_by_session(token: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
               WHERE sessions.token = ?""",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def start_stage(run_id: str, name: str) -> None:
    run = get_run_unscoped(run_id)
    stages = run["stages"]
    stages.append(
        {"name": name, "status": "running", "started_at": _now(), "finished_at": None, "output": None, "error": None}
    )
    update_fields(run_id, stages=stages)


def finish_stage(run_id: str, name: str, status: str, output=None, error: str | None = None) -> None:
    run = get_run_unscoped(run_id)
    stages = run["stages"]
    for stage in reversed(stages):
        if stage["name"] == name and stage["status"] == "running":
            stage["status"] = status
            stage["finished_at"] = _now()
            stage["output"] = output
            stage["error"] = error
            break
    update_fields(run_id, stages=stages)
