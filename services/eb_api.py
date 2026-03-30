"""
eb_api.py - Empathic Building API Client
Thin wrapper around the EB REST API.
Resolves organization ID and location ID, and fetches sensor data.
"""

import requests
from services.auth import EBAuthManager

BASE_URL = "https://eu-api.empathicbuilding.com"
TARGET_LOCATION = "Myllypuro"  # Change if needed


class EBApiClient:
    """
    Wraps all calls to the Empathic Building REST API.
    Uses EBAuthManager so every request automatically has a valid token.
    """

    def __init__(self, auth: EBAuthManager):
        self.auth = auth

    # ── Discovery ─────────────────────────────────────────────────────────────

    def get_organizations(self) -> list:
        """Return the full list of organizations the user belongs to."""
        url = f"{BASE_URL}/v1/organizations"
        response = requests.get(url, headers=self.auth.get_headers(), timeout=15)
        response.raise_for_status()
        return response.json()

    def find_location(self, location_name: str = TARGET_LOCATION) -> dict:
        """
        Search all organizations for a location matching `location_name`.
        Returns a dict with keys: id, name, organization_id
        Raises ValueError if not found.
        """
        orgs = self.get_organizations()
        for org in orgs:
            for loc in org.get("locations", []):
                if loc.get("name") == location_name:
                    return {
                        "id": loc["id"],
                        "name": loc["name"],
                        "organization_id": loc["organization_id"],
                    }
        raise ValueError(
            f"Location '{location_name}' not found. "
            f"Available: {[l['name'] for o in orgs for l in o.get('locations', [])]}"
        )

    # ── Sensor data ───────────────────────────────────────────────────────────

    def get_sensors(self, org_id: int | str, loc_id: int | str) -> list:
        """
        Fetch full sensor metadata + last_measurement for a location.
        Returns a list of raw sensor dicts from the EB API.
        """
        url = (
            f"{BASE_URL}/v1/organizations/{org_id}"
            f"/locations/{loc_id}/sensors"
        )
        response = requests.get(url, headers=self.auth.get_headers(), timeout=30)
        response.raise_for_status()
        data = response.json()

        # API may return a list directly or a wrapper dict
        if isinstance(data, list):
            return data
        return data.get("sensors", data.get("data", []))

    def get_sensor_live(self, org_id: int | str, loc_id: int | str,
                        sensor_id: int | str) -> dict:
        """Fetch the latest measurement for a single sensor."""
        url = (
            f"{BASE_URL}/v1/organizations/{org_id}"
            f"/locations/{loc_id}/sensors/{sensor_id}/measurements"
        )
        response = requests.get(
            url,
            headers=self.auth.get_headers(),
            params={"limit": 1},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()


# ── Quick sanity check ────────────────────────────────────────────────────────
if __name__ == "__main__":
    from services.auth import get_auth
    import getpass, json

    email = input("EB email: ")
    password = input("EB password: ")

    auth = get_auth(email, password)
    client = EBApiClient(auth)

    location = client.find_location()
    print(f"Found: {location}")

    sensors = client.get_sensors(location["organization_id"], location["id"])
    print(f"Total sensors: {len(sensors)}")
    print("Sample:", json.dumps(sensors[0], indent=2))
