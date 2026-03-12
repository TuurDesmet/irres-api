import requests
import json
import os
import sys
from datetime import datetime

# === CONFIGURATION ===
# Credentials
BOT_ID = os.getenv("BOT_ID")
TOKEN = os.getenv("BOTPRESS_TOKEN")

# API Endpoints
BASE_API = "https://irres-listings-api.onrender.com/api"
LISTINGS_API = f"{BASE_API}/listings"
IMAGES_API = f"{BASE_API}/office-images"
LOCATIONS_API = f"{BASE_API}/locations"

# Botpress Headers
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "x-bot-id": BOT_ID,
    "Content-Type": "application/json"
}

# === TIMEOUT CONFIGURATION ===
#
# Every timeout is written as (connect_seconds, read_seconds):
#
#   connect_seconds = how many seconds to wait for the server to ACCEPT the
#                     connection (like waiting for someone to pick up the phone).
#                     If the server doesn't respond within this time, you get a
#                     ConnectTimeout error.
#
#   read_seconds    = how many seconds to wait for the server to FINISH sending
#                     its response after the connection is open (like waiting for
#                     them to finish talking). If the server is still working and
#                     hasn't sent a complete response within this time, you get a
#                     ReadTimeout error.
#
# ── LISTINGS_TIMEOUT ──────────────────────────────────────────────────────────
#   The /api/listings endpoint is SLOW by design: it visits every property's
#   detail page one by one to collect contact info and property details.
#   With ~15–20 listings on the site, plus a polite 0.09 s delay between each
#   detail page fetch, the total scrape typically takes 3–8 minutes.
#
#   connect = 15 s  → gives Render.com's free-tier server time to cold-start
#                      (free servers spin down after inactivity and can take
#                      ~50 s to wake up, but 15 s is enough once they're awake)
#   read    = 600 s → 10 minutes; gives the scrape plenty of room to finish
#                      even if the site is slow or there are many listings.
#                      Increase this number if you ever see read timeout errors
#                      again. Decrease it if you want to fail faster.
#
LISTINGS_TIMEOUT = (15, 450)

# ── FAST_API_TIMEOUT ──────────────────────────────────────────────────────────
#   The /api/office-images and /api/locations endpoints are FAST: they scrape
#   a single page each and return immediately (typically under 5 seconds).
#
#   connect = 15 s  → same cold-start buffer as above
#   read    = 90 s  → generous buffer; these endpoints should finish in < 10 s
#                      under normal conditions. Only increase this if you see
#                      read timeout errors on office-images or locations.
#
FAST_API_TIMEOUT = (15, 90)

# ── BOTPRESS_TIMEOUT ──────────────────────────────────────────────────────────
#   Botpress Cloud is always fast and never cold-starts.
#
#   connect = 10 s  → Botpress should always accept connections quickly
#   read    = 30 s  → more than enough for any Botpress table operation
#
BOTPRESS_TIMEOUT = (10, 30)


# === VALIDATION HELPERS ===

def validate_listings_data(data: dict) -> tuple[bool, str]:
    """
    Validates the raw API response for listings.
    Returns (is_valid: bool, reason: str).
    A response is considered valid when:
      - The top-level 'success' flag is True
      - The 'listings' key exists and contains at least one item
      - Every item has a non-empty 'listing_id' and 'listing_url'
    """
    if not isinstance(data, dict):
        return False, "Response is not a JSON object."
    if not data.get('success'):
        return False, "API returned success=False."
    listings = data.get('listings')
    if not listings or not isinstance(listings, list):
        return False, "No listings array found in response."
    if len(listings) == 0:
        return False, "Listings array is empty."
    for i, item in enumerate(listings):
        if not item.get('listing_id'):
            return False, f"Item at index {i} is missing 'listing_id'."
        if not item.get('listing_url'):
            return False, f"Item at index {i} is missing 'listing_url'."
    return True, f"{len(listings)} valid listing(s) found."


def validate_office_images_data(data: dict) -> tuple[bool, str]:
    """
    Validates the raw API response for office images.
    Returns (is_valid: bool, reason: str).
    A response is considered valid when:
      - The 'data' key exists and is a non-empty dict
      - At least one key maps to a non-empty URL string
    """
    if not isinstance(data, dict):
        return False, "Response is not a JSON object."
    images = data.get('data')
    if not images or not isinstance(images, dict):
        return False, "No 'data' dict found in response."
    valid_entries = {k: v for k, v in images.items() if isinstance(v, str) and v.strip()}
    if len(valid_entries) == 0:
        return False, "All image entries are empty or missing URLs."
    return True, f"{len(valid_entries)} valid office image(s) found."


def validate_locations_data(data: dict) -> tuple[bool, str]:
    """
    Validates the raw API response for locations.
    Returns (is_valid: bool, reason: str).
    A response is considered valid when:
      - The 'data' key exists
      - 'all_locations' is a non-empty list
      - 'location_groups' is a non-empty dict
    """
    if not isinstance(data, dict):
        return False, "Response is not a JSON object."
    inner = data.get('data')
    if not inner or not isinstance(inner, dict):
        return False, "No 'data' object found in response."
    all_locations = inner.get('all_locations')
    if not all_locations or not isinstance(all_locations, list) or len(all_locations) == 0:
        return False, "'all_locations' is missing or empty."
    location_groups = inner.get('location_groups')
    if not location_groups or not isinstance(location_groups, dict) or len(location_groups) == 0:
        return False, "'location_groups' is missing or empty."
    return True, f"{len(all_locations)} location(s) across {len(location_groups)} group(s) found."


# === BOTPRESS TABLE HELPERS ===

def delete_table_rows(table_name: str) -> None:
    """
    Deletes all rows from a specified Botpress table.
    Only called AFTER data has been validated — this ensures we never
    wipe a table and then fail to repopulate it with good data.
    """
    print(f"  Clearing table '{table_name}'...")
    url = f"https://api.botpress.cloud/v1/tables/{table_name}/rows/delete"
    try:
        res = requests.post(url, headers=HEADERS, json={"deleteAllRows": True}, timeout=BOTPRESS_TIMEOUT)
        res.raise_for_status()
        print(f"  Table '{table_name}' cleared successfully.")
    except requests.exceptions.HTTPError as err:
        # Non-fatal: table may not exist yet on first run
        print(f"  Warning: Could not clear table '{table_name}': {err}")
    except Exception as e:
        print(f"  Error clearing table '{table_name}': {e}")
        raise


# === SYNC FUNCTIONS ===

def sync_listings() -> None:
    """
    Syncs Listings to ListingsTable.

    Safety flow:
      1. Fetch data from the listings API.
      2. Validate the response — if invalid, abort without touching Botpress.
      3. Only after validation passes: clear the table and insert fresh rows.
    """
    print("\n[Listings] Fetching data from API...")
    try:
        res = requests.get(LISTINGS_API, timeout=LISTINGS_TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"[Listings] ❌ Failed to fetch listings: {e}")
        print("[Listings] ⚠️  Botpress table was NOT modified.")
        return

    # --- Validate before touching Botpress ---
    is_valid, reason = validate_listings_data(data)
    if not is_valid:
        print(f"[Listings] ❌ Validation failed — {reason}")
        print("[Listings] ⚠️  Botpress table was NOT modified to prevent data loss.")
        return
    print(f"[Listings] ✅ Validation passed — {reason}")

    # Safe to proceed: clear old data and insert fresh rows
    delete_table_rows("ListingsTable")

    rows = []
    for l in data['listings']:
        # Serialize nested 'details' object to a JSON string for flat storage
        details_json = json.dumps(l.get('details', {}), ensure_ascii=False)
        row = {
            "listing_id":     l.get('listing_id'),
            "listing_url":    l.get('listing_url'),
            "photo_url":      l.get('photo_url'),
            "price":          l.get('price'),
            "location":       l.get('location'),
            "description":    l.get('description'),
            "listing_type":   l.get('listing_type'),
            "Title":          l.get('Title', ""),
            "Button1_Label":  l.get('Button1_Label', "Bekijk het op onze website"),
            "Button2_Label":  l.get('Button2_Label', ""),
            "Button2_email":  l.get('Button2_email', ""),
            "Button3_Label":  l.get('Button3_Label'),
            "Button3_Value":  l.get('Button3_Value'),
            "details":        details_json,
            "last_updated":   datetime.now().isoformat()
        }
        rows.append(row)

    try:
        insert_url = "https://api.botpress.cloud/v1/tables/ListingsTable/rows"
        res = requests.post(insert_url, headers=HEADERS, json={"rows": rows}, timeout=BOTPRESS_TIMEOUT)
        res.raise_for_status()
        print(f"[Listings] ✅ Inserted {len(rows)} listing(s) successfully.")
    except Exception as e:
        print(f"[Listings] ❌ Failed to insert listings into Botpress: {e}")


def sync_office_images() -> None:
    """
    Syncs Office Images to OfficeImagesTable.

    Safety flow:
      1. Fetch data from the office images API.
      2. Validate the response — if invalid, abort without touching Botpress.
      3. Only after validation passes: clear the table and insert fresh rows.
    """
    print("\n[OfficeImages] Fetching data from API...")
    try:
        res = requests.get(IMAGES_API, timeout=FAST_API_TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"[OfficeImages] ❌ Failed to fetch office images: {e}")
        print("[OfficeImages] ⚠️  Botpress table was NOT modified.")
        return

    # --- Validate before touching Botpress ---
    is_valid, reason = validate_office_images_data(data)
    if not is_valid:
        print(f"[OfficeImages] ❌ Validation failed — {reason}")
        print("[OfficeImages] ⚠️  Botpress table was NOT modified to prevent data loss.")
        return
    print(f"[OfficeImages] ✅ Validation passed — {reason}")

    # Safe to proceed: clear old data and insert fresh rows
    delete_table_rows("OfficeImagesTable")

    image_rows = []
    for key, url in data['data'].items():
        if isinstance(url, str) and url.strip():
            # Strip the "Irres" prefix and "Image" suffix for a clean office name
            name = key.replace("Irres", "").replace("Image", "")
            image_rows.append({"office_name": name, "image_url": url})

    try:
        insert_url = "https://api.botpress.cloud/v1/tables/OfficeImagesTable/rows"
        res = requests.post(insert_url, headers=HEADERS, json={"rows": image_rows}, timeout=BOTPRESS_TIMEOUT)
        res.raise_for_status()
        print(f"[OfficeImages] ✅ Inserted {len(image_rows)} office image(s) successfully.")
    except Exception as e:
        print(f"[OfficeImages] ❌ Failed to insert office images into Botpress: {e}")


def sync_locations() -> None:
    """
    Syncs Locations to FilterLocationsTable as a single row.
    Columns populated: 'all_locations' and 'location_groups' (both JSON strings).

    Safety flow:
      1. Fetch data from the locations API.
      2. Validate the response — if invalid, abort without touching Botpress.
      3. Only after validation passes: clear the table and insert the fresh row.
    """
    print("\n[Locations] Fetching data from API...")
    try:
        res = requests.get(LOCATIONS_API, timeout=FAST_API_TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"[Locations] ❌ Failed to fetch locations: {e}")
        print("[Locations] ⚠️  Botpress table was NOT modified.")
        return

    # --- Validate before touching Botpress ---
    is_valid, reason = validate_locations_data(data)
    if not is_valid:
        print(f"[Locations] ❌ Validation failed — {reason}")
        print("[Locations] ⚠️  Botpress table was NOT modified to prevent data loss.")
        return
    print(f"[Locations] ✅ Validation passed — {reason}")

    # Safe to proceed: clear old data and insert fresh row
    delete_table_rows("FilterLocationsTable")

    all_locations_data   = data['data'].get('all_locations', [])
    location_groups_data = data['data'].get('location_groups', {})

    # ensure_ascii=False preserves accented characters (e.g. French 'é')
    row_payload = {
        "all_locations":   json.dumps(all_locations_data,   ensure_ascii=False),
        "location_groups": json.dumps(location_groups_data, ensure_ascii=False)
    }

    try:
        insert_url = "https://api.botpress.cloud/v1/tables/FilterLocationsTable/rows"
        res = requests.post(insert_url, headers=HEADERS, json={"rows": [row_payload]}, timeout=BOTPRESS_TIMEOUT)
        res.raise_for_status()
        print("[Locations] ✅ Inserted locations and location groups into 1 row successfully.")
    except Exception as e:
        print(f"[Locations] ❌ Failed to insert locations into Botpress: {e}")


# === ENTRY POINT ===

if __name__ == "__main__":
    print("=" * 50)
    print("   Starting Botpress Sync Process")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    overall_success = True
    try:
        sync_listings()
        sync_office_images()
        sync_locations()
    except Exception as e:
        print(f"\n❌ Unexpected error during sync: {e}")
        overall_success = False
        sys.exit(1)

    if overall_success:
        print("\n" + "=" * 50)
        print("✅ Sync process completed.")
        print("=" * 50)
