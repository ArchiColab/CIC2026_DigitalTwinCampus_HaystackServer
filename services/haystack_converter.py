"""
haystack_converter.py - Raw EB Sensor → Project Haystack (Hayson) Converter

Key problem solved:
  The EB API does not reliably return a spaceID / room reference for every
  sensor.  This converter bridges the gap using the local space_mapping.json
  file (built from the IFC model) and a best-effort fuzzy match strategy.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# ── Space-matching helpers ────────────────────────────────────────────────────

SPACE_MAPPING_FILE = Path("data/space_mapping.json")


def load_space_mapping(path: Path = SPACE_MAPPING_FILE) -> list:
    if not path.exists():
        print(f"[HaystackConverter] WARNING: {path} not found – no space matching.")
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _match_space(sensor: dict, spaces: list) -> Optional[dict]:
    """
    Try to find a matching IFC space for a sensor.

    Strategy (in order of confidence):
      1. sensor['space_id']  matches space['space_number']  (EB sends it)
      2. sensor['name']      matches space['space_number']
      3. sensor['vendor_id'] matches space['space_number']
      4. sensor['name']      is contained in space['space_name']  (fuzzy)
    """
    if not spaces:
        return None

    s_name = str(sensor.get("name", ""))
    s_vendor = str(sensor.get("vendor_id", ""))
    s_space = str(sensor.get("space_id", ""))  # field may be absent

    for space in spaces:
        sp_num = str(space.get("space_number", ""))
        # Exact matches
        if s_space and s_space == sp_num:
            return space
        if s_name and s_name == sp_num:
            return space
        if s_vendor and s_vendor == sp_num:
            return space

    # Fuzzy: sensor name fragment inside space name
    for space in spaces:
        sp_name = str(space.get("space_name", "")).lower()
        if s_name and len(s_name) > 3 and s_name.lower() in sp_name:
            return space

    return None


# ── Type → Haystack tag mapping ───────────────────────────────────────────────

_TYPE_MAP = {
    "temperature": {"temp": "m:", "kind": "Number", "unit": "°C"},
    "co2":         {"co2":  "m:", "kind": "Number", "unit": "ppm"},
    "humidity":    {"humidity": "m:", "kind": "Number", "unit": "%RH"},
    "occupancy":   {"occupancy": "m:", "kind": "Bool"},
    "counter":     {"nfc":  "m:", "kind": "Number", "unit": "count"},
    "voc":         {"voc":  "m:", "kind": "Number", "unit": "ppb"},
    "light":       {"light": "m:", "kind": "Number", "unit": "lux"},
    "noise":       {"noise": "m:", "kind": "Number", "unit": "dB"},
    "pm25":        {"pm25": "m:", "kind": "Number", "unit": "µg/m³"},
}


def _get_cur_val(sensor: dict, s_type: str):
    """Extract current value from last_measurement, handling type quirks."""
    last = sensor.get("last_measurement") or {}
    if s_type == "occupancy":
        return bool(last.get("used", 0) > 0) if "used" in last else None
    return last.get("value")


# ── Main transform ────────────────────────────────────────────────────────────

def transform_sensor(sensor: dict, space_mapping: list) -> dict:
    """Convert one raw EB sensor dict into a Project Haystack (Hayson) entity."""
    s_id   = sensor.get("id", "unknown")
    s_type = sensor.get("type", "unknown")
    s_name = sensor.get("name") or f"{s_type}-{s_id}"

    entity: dict = {
        "id":       f"r:{s_id}",
        "dis":      s_name,
        "point":    "m:",
        "sensor":   "m:",
        "siteRef":  "r:Myllypuro_Campus",
        # EB-specific metadata kept alongside Haystack tags
        "eb_id":        str(s_id),
        "eb_type":      s_type,
        "eb_vendor":    sensor.get("vendor"),
        "eb_vendor_id": sensor.get("vendor_id"),
    }

    # ── Space reference (the missing-location workaround) ────────────────────
    space = _match_space(sensor, space_mapping)
    if space:
        entity["spaceRef"] = f"r:{space.get('space_guid', space.get('space_number'))}"
        entity["floorRef"] = f"r:{space.get('floor_name', 'unknown_floor')}"
        entity["dis_space"] = space.get("space_name", "")
    else:
        # No IFC match – flag it so Blender can show an "unlocated" indicator
        entity["unlocated"] = "m:"

    # ── Semantic tags based on sensor type ───────────────────────────────────
    tags = _TYPE_MAP.get(s_type, {})
    entity.update(tags)

    # ── Current value ────────────────────────────────────────────────────────
    cur_val = _get_cur_val(sensor, s_type)
    if cur_val is not None:
        entity["curVal"]    = cur_val
        entity["cur"]       = "m:"
        entity["curStatus"] = "ok"

    # ── Timestamp ────────────────────────────────────────────────────────────
    last = sensor.get("last_measurement") or {}
    ts = last.get("measured_at") or last.get("timestamp")
    if ts:
        entity["lastUpdate"] = ts

    return entity


def convert_sensors(raw_sensors: list, space_mapping: list) -> list:
    """Convert a full list of raw EB sensors to a Haystack model list."""
    return [transform_sensor(s, space_mapping) for s in raw_sensors]


def print_summary(haystack_model: list) -> None:
    """Print a quick diagnostic summary after conversion."""
    total      = len(haystack_model)
    located    = sum(1 for e in haystack_model if "spaceRef" in e)
    unlocated  = total - located
    by_type    = {}
    for e in haystack_model:
        t = e.get("eb_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    print(f"\n[HaystackConverter] Conversion summary")
    print(f"  Total sensors : {total}")
    print(f"  Located (IFC) : {located}  ({100*located//total if total else 0}%)")
    print(f"  Unlocated     : {unlocated}")
    print(f"  By type       : {by_type}\n")


# ── Standalone usage ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import glob, os

    DATA_DIR = "data"
    spaces   = load_space_mapping()

    raw_files = glob.glob(os.path.join(DATA_DIR, "raw_metadata_loc*.json"))
    if not raw_files:
        print("No raw_metadata files found in data/. Run collector.py first.")
    else:
        latest = max(raw_files)
        with open(latest) as f:
            raw = json.load(f)
        model = convert_sensors(raw, spaces)
        print_summary(model)

        out = os.path.join(DATA_DIR, "haystack_model_latest.json")
        with open(out, "w") as f:
            json.dump(model, f, indent=2, ensure_ascii=False)
        print(f"Saved to {out}")
