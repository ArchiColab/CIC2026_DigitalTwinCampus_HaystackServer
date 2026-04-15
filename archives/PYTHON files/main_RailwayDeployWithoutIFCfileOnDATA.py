"""
main.py — Campus Digital Twin — FastAPI Backend
"""

import json
import logging
import os
import secrets
import traceback
from datetime import datetime, timezone

import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from services.auth import EBAuthManager
from services.eb_api import EBApiClient
from services.haystack_converter import convert_sensors, load_space_mapping

# ── Logging — shows up in Railway's log tab ───────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("campus-dt")

# ── Configuration ─────────────────────────────────────────────────────────────
SESSION_TTL     = 60 * 60 * 8   # 8 hours
USER_CACHE_TTL  = 60 * 3        # 3 minutes
TARGET_LOCATION = "Myllypuro"

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Campus Digital Twin API")

# ── Redis ─────────────────────────────────────────────────────────────────────
redis_url = os.environ.get("REDIS_URL")
if not redis_url:
    raise RuntimeError(
        "REDIS_URL is not set. Add a Redis plugin in your Railway project "
        "(New → Database → Add Redis)."
    )

try:
    r = redis.from_url(redis_url, decode_responses=True)
    r.ping()
    log.info("Redis connected: %s", redis_url.split("@")[-1])   # log host, not password
except Exception as e:
    raise RuntimeError(f"Cannot connect to Redis: {e}") from e

# ── IFC space mapping ─────────────────────────────────────────────────────────
spaces = load_space_mapping()
log.info("Space mapping loaded: %d spaces", len(spaces))


# ── Session helpers ────────────────────────────────────────────────────────────

def get_session(request: Request) -> dict:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    data = r.get(f"session:{token}")
    if not data:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    return json.loads(data)


def auth_from_session(session: dict) -> EBAuthManager:
    auth             = EBAuthManager.__new__(EBAuthManager)
    auth.email       = session["email"]
    auth.password    = ""
    auth._token      = {
        "access_token":  session["access_token"],
        "token_type":    session["token_type"],
        "refresh_token": session.get("refresh_token", ""),
    }
    auth._expires_at = session["expires_at"]
    return auth


# ── Health / debug — NO auth required ─────────────────────────────────────────

@app.get("/api/health")
async def health():
    """
    First thing to visit when debugging Railway issues.
    URL: https://your-app.up.railway.app/api/health

    Shows the status of every dependency — Redis, space mapping, EB API.
    No login required so you can check it before credentials work.
    """
    result = {
        "status":    "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "redis":     None,
        "space_mapping":    None,
        "eb_api_reachable": None,
        "env": {
            "REDIS_URL_set": bool(os.environ.get("REDIS_URL")),
            "PORT":          os.environ.get("PORT", "not set"),
        },
    }

    # 1. Redis
    try:
        r.ping()
        result["redis"] = "connected"
    except Exception as e:
        result["redis"]  = f"ERROR: {e}"
        result["status"] = "degraded"

    # 2. Space mapping
    try:
        result["space_mapping"] = f"{len(spaces)} spaces loaded"
    except Exception as e:
        result["space_mapping"] = f"ERROR: {e}"
        result["status"]        = "degraded"

    # 3. EB API reachability — just checks the server is up, no credentials needed
    try:
        import requests as req_lib
        resp = req_lib.get(
            "https://eu-api.empathicbuilding.com/v1/organizations",
            timeout = 5,
        )
        # 401 = server is up and responding, we just have no token — that's fine
        result["eb_api_reachable"] = f"HTTP {resp.status_code} (401 = server up)"
    except Exception as e:
        result["eb_api_reachable"] = f"ERROR: {e}"
        result["status"]           = "degraded"

    return result


# ── Login ─────────────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(request: Request):
    body     = await request.json()
    email    = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    log.info("Login attempt: %s", email)

    # Step 1: authenticate against EB API
    try:
        auth = EBAuthManager(email, password)
        auth.force_login()
        log.info("EB login OK: %s", email)

    except Exception as e:
        # Full traceback goes to Railway logs so you can see the real cause
        log.error(
            "EB login FAILED for %s\n"
            "  type : %s\n"
            "  msg  : %s\n"
            "  trace:\n%s",
            email, type(e).__name__, str(e), traceback.format_exc(),
        )
        # Return the actual error to the browser — much easier to debug than
        # a generic "invalid credentials" message
        raise HTTPException(
            status_code = 401,
            detail      = f"EB login failed ({type(e).__name__}): {e}",
        )

    # Step 2: write session to Redis
    try:
        session_token = secrets.token_hex(32)
        r.setex(
            f"session:{session_token}",
            SESSION_TTL,
            json.dumps({
                "email":         email,
                "access_token":  auth._token["access_token"],
                "token_type":    auth._token["token_type"],
                "refresh_token": auth._token.get("refresh_token", ""),
                "expires_at":    auth._expires_at,
                "logged_in_at":  datetime.now(timezone.utc).isoformat(),
            }),
        )
        log.info("Session stored for %s (TTL %ds)", email, SESSION_TTL)

    except Exception as e:
        log.error("Redis write failed: %s\n%s", e, traceback.format_exc())
        raise HTTPException(
            status_code = 500,
            detail      = f"Session store failed ({type(e).__name__}): {e}",
        )

    response = JSONResponse({"ok": True, "email": email})
    response.set_cookie(
        key      = "session",
        value    = session_token,
        httponly = True,
        samesite = "strict",
        secure   = True,      # HTTPS — Railway provides TLS automatically
        max_age  = SESSION_TTL,
    )
    return response


# ── Logout ────────────────────────────────────────────────────────────────────

@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        session_data = r.get(f"session:{token}")
        if session_data:
            email = json.loads(session_data).get("email", "")
            r.delete(f"session:{token}")
            r.delete(f"cache:{email}")
            log.info("Logged out: %s", email)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


# ── Session check ─────────────────────────────────────────────────────────────

@app.get("/api/me")
async def me(session: dict = Depends(get_session)):
    return {"email": session["email"]}


# ── Sensor data ───────────────────────────────────────────────────────────────

@app.get("/api/points")
async def get_points(session: dict = Depends(get_session)):
    email     = session["email"]
    cache_key = f"cache:{email}"

    cached = r.get(cache_key)
    if cached:
        payload = json.loads(cached)
        log.info("Cache hit for %s", email)
        return JSONResponse(
            content = payload["data"],
            headers = {"X-Cache-Updated": payload["updated_at"]},
        )

    log.info("Cache miss for %s — fetching from EB", email)
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
        log.info("Fetched %d sensors for %s", len(data), email)

        return JSONResponse(
            content = data,
            headers = {"X-Cache-Updated": now},
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error("Sensor fetch failed for %s: %s\n%s", email, e, traceback.format_exc())
        raise HTTPException(
            status_code = 502,
            detail      = f"EB sensor fetch failed ({type(e).__name__}): {e}",
        )


# ── Status (debug, requires login) ───────────────────────────────────────────

@app.get("/api/status")
async def status(request: Request, session: dict = Depends(get_session)):
    email     = session["email"]
    cache_key = f"cache:{email}"
    ttl       = r.ttl(cache_key)
    cached    = r.get(cache_key)
    updated   = json.loads(cached)["updated_at"] if cached else None
    token     = request.cookies.get("session", "")

    return {
        "email":            email,
        "cache_exists":     bool(cached),
        "cache_expires_in": f"{ttl}s" if ttl > 0 else "expired",
        "last_updated":     updated,
        "session_ttl":      r.ttl(f"session:{token}"),
    }


# ── Frontend — must be last so API routes take priority ───────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
