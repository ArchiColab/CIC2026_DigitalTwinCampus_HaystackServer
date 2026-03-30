"""
auth.py - Empathic Building Authentication Service
Handles login, token storage, and automatic token refresh.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
BASE_URL = os.environ.get("API_BASE_URL", "https://eu-api.empathicbuilding.com")
TOKEN_FILE = Path("data/token.json")

# ── Token Manager ─────────────────────────────────────────────────────────────
class EBAuthManager:
    """
    Manages authentication with the Empathic Building API.
    Handles:
      - Initial login with email/password
      - Token persistence to disk (data/token.json)
      - Automatic refresh before expiry
    """

    def __init__(self, email: str = None, password: str = None):
        self.email = email or os.environ.get("EB_EMAIL", "")
        self.password = password or os.environ.get("EB_PASSWORD", "")
        self._token: dict = {}
        self._expires_at: float = 0.0

        # Try loading a saved token on startup
        self._load_token()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_headers(self) -> dict:
        """Return ready-to-use Authorization headers, refreshing token if needed."""
        self._ensure_valid_token()
        return {
            "Authorization": f"{self._token['token_type']} {self._token['access_token']}",
            "Accept": "application/json",
        }

    def force_login(self) -> None:
        """Explicitly perform a fresh login (ignores any cached token)."""
        self._login()

    # ── Internal Logic ────────────────────────────────────────────────────────

    def _ensure_valid_token(self) -> None:
        """Refresh or login if token is missing or about to expire (< 60 s buffer)."""
        now = time.time()
        if not self._token:
            print("[Auth] No token in memory – logging in...")
            self._login()
        elif now >= self._expires_at - 60:
            print("[Auth] Token expiring soon – refreshing...")
            try:
                self._refresh()
            except Exception as e:
                print(f"[Auth] Refresh failed ({e}) – falling back to login...")
                self._login()

    def _login(self) -> None:
        if not self.email or not self.password:
            raise ValueError(
                "EB_EMAIL and EB_PASSWORD must be set in .env or as environment variables."
            )
        response = requests.post(
            f"{BASE_URL}/v1/login",
            data={"email": self.email, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        response.raise_for_status()
        self._store_token(response.json())
        print("[Auth] Login successful.")

    def _refresh(self) -> None:
        response = requests.post(
            f"{BASE_URL}/v1/token",
            data={"refresh_token": self._token["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        response.raise_for_status()
        self._store_token(response.json())
        print("[Auth] Token refreshed.")

    def _store_token(self, token_data: dict) -> None:
        """Save token in memory and persist to disk."""
        self._token = token_data
        # expires_in is in seconds from now
        self._expires_at = time.time() + token_data.get("expires_in", 3600)
        token_data["_expires_at"] = self._expires_at
        token_data["_saved_at"] = datetime.now(timezone.utc).isoformat()

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)

    def _load_token(self) -> None:
        """Load token from disk if it still has some life left."""
        if not TOKEN_FILE.exists():
            return
        try:
            with open(TOKEN_FILE) as f:
                token_data = json.load(f)
            expires_at = token_data.get("_expires_at", 0)
            if time.time() < expires_at - 60:
                self._token = token_data
                self._expires_at = expires_at
                print("[Auth] Loaded valid token from disk.")
            else:
                print("[Auth] Cached token expired – will re-authenticate on first use.")
        except Exception as e:
            print(f"[Auth] Could not load cached token: {e}")


# ── Singleton helper used by other modules ────────────────────────────────────
_auth_manager: EBAuthManager | None = None


def get_auth(email: str = None, password: str = None) -> EBAuthManager:
    """Return (or create) the shared EBAuthManager singleton."""
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = EBAuthManager(email, password)
    return _auth_manager


# ── CLI quick-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    auth = get_auth()
    headers = auth.get_headers()
    print("Headers ready:", headers)
