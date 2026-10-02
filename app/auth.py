"""Password hashing + session cookie helpers.

Not hardened for production (no rate limiting, no password reset flow) --
this is a single-operator demo tool, so the bar is "don't store plaintext
passwords," not "survive a security audit."
"""
import hashlib
import hmac
import os

from fastapi import HTTPException, Request

SESSION_COOKIE = "session_token"
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split(":")
    salt = bytes.fromhex(salt_hex)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(candidate.hex(), digest_hex)


def require_auth(request: Request) -> dict:
    from app import db  # local import avoids a circular import with main.py

    token = request.cookies.get(SESSION_COOKIE)
    user = db.get_user_by_session(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def admin_dashboard_restricted() -> bool:
    """Controlled by ADMIN_DASHBOARD_ACCESS: 'everyone' (default) or 'admin_only'.
    When 'admin_only', ADMIN_EMAILS (comma-separated) decides who qualifies."""
    return os.environ.get("ADMIN_DASHBOARD_ACCESS", "everyone").strip().lower() == "admin_only"


def require_admin_dashboard(request: Request) -> dict:
    user = require_auth(request)
    if admin_dashboard_restricted() and user["email"].lower() not in admin_emails():
        raise HTTPException(status_code=403, detail="admin access required")
    return user
