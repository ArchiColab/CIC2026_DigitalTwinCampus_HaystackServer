"""
collector.py - Data Collector Service

Responsibilities:
  1. Authenticate with EB (via auth.py)
  2. Discover organization_id + location_id
  3. Fetch sensor data at a configurable interval
  4. Convert to Haystack model (via haystack_converter.py)
  5. Save timestamped raw + Haystack JSON files to data/
  6. Keep a stable "latest" file that Blender always reads from

Resilience:
  - Token refresh is handled automatically by EBAuthManager
  - If a fetch fails, the previous dataset is untouched (Blender keeps working)
  - If Blender crashes, the collector keeps running independently
"""

import json
import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

from services.auth import get_auth
from services.eb_api import EBApiClient
from services.haystack_converter import load_space_mapping, convert_sensors, print_summary

# ── Configuration ─────────────────────────────────────────────────────────────
DATA_DIR           = Path("data")
LATEST_RAW_FILE    = DATA_DIR / "raw_latest.json"
LATEST_HAYSTACK    = DATA_DIR / "haystack_latest.json"
TARGET_LOCATION    = "Myllypuro"
POLL_INTERVAL_SEC  = 300   # 5 minutes – adjust as needed
KEEP_HISTORY       = True   # Set False to only keep "latest" files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("collector")


# ── Collector class ───────────────────────────────────────────────────────────

class DataCollector:

    def __init__(self, email: str = None, password: str = None):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.auth   = get_auth(email, password)
        self.client = EBApiClient(self.auth)
        self.spaces = load_space_mapping()

        # Resolved once on startup (or refreshed if not found)
        self.org_id  = None
        self.loc_id  = None

    # ── Setup ─────────────────────────────────────────────────────────────────

    def resolve_location(self) -> None:
        """Find and store org_id / loc_id for the target campus."""
        log.info(f"Resolving location: '{TARGET_LOCATION}'...")
        location = self.client.find_location(TARGET_LOCATION)
        self.org_id = location["organization_id"]
        self.loc_id = location["id"]
        log.info(f"  org_id={self.org_id}, loc_id={self.loc_id}")

    # ── Single fetch cycle ────────────────────────────────────────────────────

    def fetch_once(self) -> bool:
        """
        Perform one data fetch + Haystack conversion + save.
        Returns True on success, False on failure (previous data preserved).
        """
        if self.org_id is None:
            self.resolve_location()

        try:
            log.info("Fetching sensor data from EB API...")
            raw_sensors = self.client.get_sensors(self.org_id, self.loc_id)
            log.info(f"  Received {len(raw_sensors)} sensors.")

            # Convert to Haystack
            haystack_model = convert_sensors(raw_sensors, self.spaces)
            print_summary(haystack_model)

            # Save files
            self._save(raw_sensors, haystack_model)
            return True

        except Exception as e:
            log.error(f"Fetch failed: {e}  (previous dataset preserved)")
            return False

    # ── Save helpers ──────────────────────────────────────────────────────────

    def _save(self, raw: list, haystack: list) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

        # Always write "latest" files (Blender reads these)
        self._write_json(LATEST_RAW_FILE, raw)
        self._write_json(LATEST_HAYSTACK, haystack)
        log.info(f"  Updated: {LATEST_HAYSTACK}")

        # Optionally keep timestamped history
        if KEEP_HISTORY:
            self._write_json(
                DATA_DIR / f"raw_metadata_loc{self.loc_id}_{timestamp}.json", raw
            )
            self._write_json(
                DATA_DIR / f"haystack_model_loc{self.loc_id}_{timestamp}.json", haystack
            )

    @staticmethod
    def _write_json(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp then rename – atomic on most OSes (Blender won't read half-written)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)

    # ── Polling loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Run the collector in a continuous loop.
        Ctrl-C to stop cleanly.
        """
        log.info("=== Data Collector Service started ===")
        log.info(f"  Poll interval : {POLL_INTERVAL_SEC}s")
        log.info(f"  Output dir    : {DATA_DIR.resolve()}")

        while True:
            self.fetch_once()
            log.info(f"Next fetch in {POLL_INTERVAL_SEC}s  (Ctrl-C to stop)")
            time.sleep(POLL_INTERVAL_SEC)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import getpass

    email    = os.environ.get("EB_EMAIL")    or input("EB email: ")
    password = os.environ.get("EB_PASSWORD") or getpass.getpass("EB password: ")

    collector = DataCollector(email, password)
    try:
        collector.run()
    except KeyboardInterrupt:
        log.info("Collector stopped by user.")
