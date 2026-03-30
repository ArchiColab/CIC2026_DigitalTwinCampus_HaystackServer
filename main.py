"""
main.py — Campus Digital Twin — FastAPI Backend
------------------------------------------------
Responsibilities:
  - Serve the IFC viewer frontend (static/index.html)
  - Authenticate users against the Empathic Building API
  - Store sessions in Redis (survives restarts, supports many users)
  - Cache sensor data per user for USER_CACHE_TTL seconds
    (prevents hammering the EB API when a whole class is online)

Environment variables required (set in Railway dashboard):
  REDIS_URL   — injected automatically by the Railway Redis plugin
  SECRET_KEY  — any random string, used for extra security checks
                (generate with: python -c "import secrets; print(secrets.token_hex(32))")

Usage:
  uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import json
import os
import secrets
from datetime import datetime, timezone

import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from services.auth import EBAuthManager
from services.eb_api import EBApiClient
from services.haystack_converter import convert_sensors, load_space_mapping

# ── Configuration ─────────────────────────────────────────────────────────────
SESSION_TTL    = 60 * 60 * 8   # 8 hours — how long a login lasts
USER_CACHE_TTL = 60 * 3        # 3 minutes — how stale sensor data can be
TARGET_LOCATION = "Myllypuro"

# ── App & Redis ───────────────────────────────────────────────────────────────
app = FastAPI(title="Campus Digital Twin API")

redis_url = os.environ.get("REDIS_URL")
if not redis_url:
    raise RuntimeError(
        "REDIS_URL environment variable is not set. "
        "Add a Redis plugin in your Railway project."
    )
r = redis.from_url(redis_url, decode_responses=True)

# Load IFC space mapping once at startup (file doesn't change at runtime)
spaces = load_space_mapping()


# ── Session helpers ────────────────────────────────────────────────────────────

def get_session(request: Request) -> dict:
    """
    Dependency — injects the current user's session dict into route handlers.
    Raises 401 if the cookie is missing or the session has expired in Redis.
    """
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    data = r.get(f"session:{token}")
    if not data:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    return json.loads(data)


def auth_from_session(session: dict) -> EBAuthManager:
    """Reconstruct an EBAuthManager from a stored session (no re-login needed)."""
    auth             = EBAuthManager.__new__(EBAuthManager)
    auth.email       = session["email"]
    auth.password    = ""          # not stored — not needed after initial login
    auth._token      = {
        "access_token": session["access_token"],
        "token_type":   session["token_type"],
        "refresh_token": session.get("refresh_token", ""),
    }
    auth._expires_at = session["expires_at"]
    return auth


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(request: Request):
    """
    Authenticate a user against the Empathic Building API.
    On success, sets an httponly session cookie valid for SESSION_TTL seconds.
    """
    body     = await request.json()
    email    = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    # Validate credentials against the real EB API
    try:
        auth = EBAuthManager(email, password)
        auth.force_login()
    except Exception:
        # Don't reveal whether it's the email or password that's wrong
        raise HTTPException(status_code=401, detail="Invalid Empathic Building credentials")

    # Store session in Redis
    session_token = secrets.token_hex(32)
    session_data  = {
        "email":         email,
        "access_token":  auth._token["access_token"],
        "token_type":    auth._token["token_type"],
        "refresh_token": auth._token.get("refresh_token", ""),
        "expires_at":    auth._expires_at,
        "logged_in_at":  datetime.now(timezone.utc).isoformat(),
    }
    r.setex(f"session:{session_token}", SESSION_TTL, json.dumps(session_data))

    response = JSONResponse({"ok": True, "email": email})
    response.set_cookie(
        key      = "session",
        value    = session_token,
        httponly = True,      # JS cannot read this cookie — XSS-safe
        samesite = "none",
        secure   = True,      # HTTPS only (Railway provides this automatically)
        max_age  = SESSION_TTL,
    )
    return response


@app.post("/api/logout")
async def logout(request: Request):
    """Delete the session from Redis and clear the browser cookie."""
    token = request.cookies.get("session")
    if token:
        session = r.get(f"session:{token}")
        if session:
            email = json.loads(session).get("email", "")
            r.delete(f"session:{token}")
            r.delete(f"cache:{email}")   # also clear their sensor cache
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


@app.get("/api/me")
async def me(session: dict = Depends(get_session)):
    """
    Quick check — returns the logged-in user's email.
    The frontend calls this on page load to decide whether to show
    the login screen or go straight to the viewer.
    """
    return {"email": session["email"]}


@app.get("/api/points")
async def get_points(session: dict = Depends(get_session)):
    """
    Return Haystack sensor data for the current user.

    Flow:
      1. Check Redis for a fresh cached result (< USER_CACHE_TTL seconds old)
      2. On cache hit  → return immediately (fast, no EB API call)
      3. On cache miss → fetch live from EB, cache the result, return it

    The X-Cache-Updated response header tells the frontend when the data
    was last fetched so it can show a "Data from N seconds ago" badge.
    """
    email     = session["email"]
    cache_key = f"cache:{email}"

    cached = r.get(cache_key)
    if cached:
        payload = json.loads(cached)
        return JSONResponse(
            content = payload["data"],
            headers = {"X-Cache-Updated": payload["updated_at"]},
        )

    # Cache miss — go to the EB API
    try:
        auth   = auth_from_session(session)
        client = EBApiClient(auth)
        loc    = client.find_location(TARGET_LOCATION)
        raw    = client.get_sensors(loc["organization_id"], loc["id"])
        data   = convert_sensors(raw, spaces)
        now    = datetime.now(timezone.utc).isoformat()

        r.setex(cache_key, USER_CACHE_TTL, json.dumps({
            "data":       data,
            "updated_at": now,
        }))

        return JSONResponse(
            content = data,
            headers = {"X-Cache-Updated": now},
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code = 502,
            detail      = f"Could not fetch data from Empathic Building: {e}",
        )


@app.get("/api/status")
async def status(session: dict = Depends(get_session)):
    """
    Debug endpoint — shows cache state for the current user.
    Useful during demos: visit /api/status to check if data is fresh.
    """
    email     = session["email"]
    cache_key = f"cache:{email}"
    ttl       = r.ttl(cache_key)
    cached    = r.get(cache_key)
    updated   = json.loads(cached)["updated_at"] if cached else None

    return {
        "email":             email,
        "cache_exists":      bool(cached),
        "cache_expires_in":  f"{ttl}s" if ttl > 0 else "expired",
        "last_updated":      updated,
        "session_ttl":       r.ttl(f"session:{request.cookies.get('session', '')}"),
    }


# ── Serve frontend — must be last so API routes take priority ──────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
