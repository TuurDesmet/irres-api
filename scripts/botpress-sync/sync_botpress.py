import requests
import json
import os
import sys
from datetime import datetime

# === CONFIGURATION ===
BOT_ID = os.getenv("BOT_ID")
TOKEN = os.getenv("BOTPRESS_TOKEN")
IRRES_API_KEY = os.getenv("IRRES_API_KEY") or os.getenv("API_KEY")

BASE_API = "https://irres-api.onrender.com/api"
LISTINGS_API = f"{BASE_API}/listings"
IMAGES_API = f"{BASE_API}/office-images"
LOCATIONS_API = f"{BASE_API}/locations"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "x-bot-id": BOT_ID,
    "Content-Type": "application/json"
}

IRRES_API_HEADERS = {"X-API-KEY": IRRES_API_KEY} if IRRES_API_KEY else {}

# === TIMEOUT CONFIGURATION ===
# (connect_seconds, read_seconds)
LISTINGS_TIMEOUT = (15, 450)   # /api/listings is slow: visits every detail page
FAST_API_TIMEOUT = (15, 90)    # /api/office-images and /api/locations are fast
BOTPRESS_TIMEOUT = (10, 30)    # Botpress Cloud is always fast

# === DISPLAY HELPERS ===

W = 50  # Line width for separators

def line():
    print("=" * W)

def divider():
    print("-" * W)

def step(label, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {label:<20} {status}  {detail}")

def step_skip(label, reason="Skipped"):
    print(f"  {label:<20} ⏭️   {reason}")

def section_header(title):
    print(f"\n{title}")

def section_result(ok):
    status = "✅  SUCCESS" if ok else "❌  FAILED"
    divider()
    print(f"  {'Result':<20} {status}")
    line()


# === VALIDATION HELPERS ===

def validate_listings_data(data):
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
    return True, f"{len(listings)} listings found"


def validate_office_images_data(data):
    if not isinstance(data, dict):
        return False, "Response is not a JSON object."
    images = data.get('data')
    if not images or not isinstance(images, dict):
        return False, "No 'data' dict found in response."
    valid_entries = {k: v for k, v in images.items() if isinstance(v, str) and v.strip()}
    if len(valid_entries) == 0:
        return False, "All image entries are empty or missing URLs."
    return True, f"{len(valid_entries)} images found"


def validate_locations_data(data):
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
    return True, f"{len(all_locations)} locations across {len(location_groups)} groups"


# === BOTPRESS TABLE HELPERS ===

def delete_table_rows(table_name):
    """
    Deletes all rows from a Botpress table.
    Returns (success: bool, detail: str).
    """
    url = f"https://api.botpress.cloud/v1/tables/{table_name}/rows/delete"
    try:
        res = requests.post(
            url,
            headers=HEADERS,
            json={"deleteAllRows": True},
            timeout=BOTPRESS_TIMEOUT
        )
        res.raise_for_status()
        return True, f"{table_name} cleared"
    except requests.exceptions.HTTPError as err:
        return False, f"HTTP {err.response.status_code}: {err.response.text}"
    except Exception as e:
        return False, str(e)


# === SYNC FUNCTIONS ===

def sync_listings():
    """
    Fetches all listings from the IRRES API and inserts them into ListingsTable.
    Returns True if fully successful, False otherwise.
    """
    section_header("LISTINGS")

    # --- Check API key ---
    if not IRRES_API_KEY:
        step("Fetch API",     False, "IRRES_API_KEY is not set")
        step_skip("Validate data")
        step_skip("Clear table")
        step_skip("Insert data")
        section_result(False)
        return False

    # --- Step 1: Fetch ---
    try:
        res = requests.get(LISTINGS_API, headers=IRRES_API_HEADERS, timeout=LISTINGS_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        fetch_ok = True
        fetch_detail = f"{len(data.get('listings', []))} listings fetched"
    except Exception as e:
        step("Fetch API", False, str(e))
        step_skip("Validate data", "Skipped (fetch failed)")
        step_skip("Clear table",   "Skipped (fetch failed)")
        step_skip("Insert data",   "Skipped (fetch failed)")
        section_result(False)
        return False

    step("Fetch API", fetch_ok, fetch_detail)

    # --- Step 2: Validate ---
    is_valid, reason = validate_listings_data(data)
    step("Validate data", is_valid, reason)
    if not is_valid:
        step_skip("Clear table", "Skipped (validation failed)")
        step_skip("Insert data", "Skipped (validation failed)")
        section_result(False)
        return False

    # --- Step 3: Clear table ---
    clear_ok, clear_detail = delete_table_rows("ListingsTable")
    step("Clear table", clear_ok, clear_detail)
    if not clear_ok:
        step_skip("Insert data", "Skipped (clear failed)")
        section_result(False)
        return False

    # --- Step 4: Insert ---
    rows = []
    for l in data['listings']:
        details_json = json.dumps(l.get('details', {}), ensure_ascii=False)
        rows.append({
            "listing_id":    l.get('listing_id'),
            "listing_url":   l.get('listing_url'),
            "photo_url":     l.get('photo_url'),
            "price":         l.get('price'),
            "location":      l.get('location'),
            "description":   l.get('description'),
            "listing_type":  l.get('listing_type'),
            "Title":         l.get('Title', ""),
            "Button1_Label": l.get('Button1_Label', "Bekijk het op onze website"),
            "Button2_Label": l.get('Button2_Label', ""),
            "Button2_email": l.get('Button2_email', ""),
            "Button3_Label": l.get('Button3_Label'),
            "Button3_Value": l.get('Button3_Value'),
            "details":       details_json,
            "last_updated":  datetime.now().isoformat()
        })

    try:
        insert_url = "https://api.botpress.cloud/v1/tables/ListingsTable/rows"
        res = requests.post(
            insert_url,
            headers=HEADERS,
            json={"rows": rows},
            timeout=BOTPRESS_TIMEOUT
        )
        res.raise_for_status()
        step("Insert data", True, f"{len(rows)} rows inserted")
        section_result(True)
        return True
    except requests.exceptions.HTTPError as e:
        step("Insert data", False, f"HTTP {e.response.status_code}: {e.response.text}")
        section_result(False)
        return False
    except Exception as e:
        step("Insert data", False, str(e))
        section_result(False)
        return False


def sync_office_images():
    """
    Fetches office images from the IRRES API and inserts them into OfficeImagesTable.
    Returns True if fully successful, False otherwise.
    """
    section_header("OFFICE IMAGES")

    # --- Check API key ---
    if not IRRES_API_KEY:
        step("Fetch API",     False, "IRRES_API_KEY is not set")
        step_skip("Validate data")
        step_skip("Clear table")
        step_skip("Insert data")
        section_result(False)
        return False

    # --- Step 1: Fetch ---
    try:
        res = requests.get(IMAGES_API, headers=IRRES_API_HEADERS, timeout=FAST_API_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        fetch_detail = f"{len(data.get('data', {}))} images fetched"
    except Exception as e:
        step("Fetch API", False, str(e))
        step_skip("Validate data", "Skipped (fetch failed)")
        step_skip("Clear table",   "Skipped (fetch failed)")
        step_skip("Insert data",   "Skipped (fetch failed)")
        section_result(False)
        return False

    step("Fetch API", True, fetch_detail)

    # --- Step 2: Validate ---
    is_valid, reason = validate_office_images_data(data)
    step("Validate data", is_valid, reason)
    if not is_valid:
        step_skip("Clear table", "Skipped (validation failed)")
        step_skip("Insert data", "Skipped (validation failed)")
        section_result(False)
        return False

    # --- Step 3: Clear table ---
    clear_ok, clear_detail = delete_table_rows("OfficeImagesTable")
    step("Clear table", clear_ok, clear_detail)
    if not clear_ok:
        step_skip("Insert data", "Skipped (clear failed)")
        section_result(False)
        return False

    # --- Step 4: Insert ---
    image_rows = []
    for key, url in data['data'].items():
        if isinstance(url, str) and url.strip():
            name = key.replace("Irres", "").replace("Image", "")
            image_rows.append({"office_name": name, "image_url": url})

    try:
        insert_url = "https://api.botpress.cloud/v1/tables/OfficeImagesTable/rows"
        res = requests.post(
            insert_url,
            headers=HEADERS,
            json={"rows": image_rows},
            timeout=BOTPRESS_TIMEOUT
        )
        res.raise_for_status()
        step("Insert data", True, f"{len(image_rows)} rows inserted")
        section_result(True)
        return True
    except requests.exceptions.HTTPError as e:
        step("Insert data", False, f"HTTP {e.response.status_code}: {e.response.text}")
        section_result(False)
        return False
    except Exception as e:
        step("Insert data", False, str(e))
        section_result(False)
        return False


def sync_locations():
    """
    Fetches locations from the IRRES API and stores them in FilterLocationsTable.

    Each location group gets its own row:
      label (string) - display name,  e.g. "Gent + deelgemeenten"
      value (string) - JSON array,    e.g. '["Gent", "Mariakerke", ...]'

    Returns True if fully successful, False otherwise.
    """
    section_header("LOCATIONS")

    # --- Check API key ---
    if not IRRES_API_KEY:
        step("Fetch API",     False, "IRRES_API_KEY is not set")
        step_skip("Validate data")
        step_skip("Clear table")
        step_skip("Insert data")
        section_result(False)
        return False

    # --- Step 1: Fetch ---
    try:
        res = requests.get(LOCATIONS_API, headers=IRRES_API_HEADERS, timeout=FAST_API_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        groups = data.get('data', {}).get('location_groups', {})
        fetch_detail = f"{len(groups)} location groups fetched"
    except Exception as e:
        step("Fetch API", False, str(e))
        step_skip("Validate data", "Skipped (fetch failed)")
        step_skip("Clear table",   "Skipped (fetch failed)")
        step_skip("Insert data",   "Skipped (fetch failed)")
        section_result(False)
        return False

    step("Fetch API", True, fetch_detail)

    # --- Step 2: Validate ---
    is_valid, reason = validate_locations_data(data)
    step("Validate data", is_valid, reason)
    if not is_valid:
        step_skip("Clear table", "Skipped (validation failed)")
        step_skip("Insert data", "Skipped (validation failed)")
        section_result(False)
        return False

    # --- Step 3: Clear table ---
    clear_ok, clear_detail = delete_table_rows("FilterLocationsTable")
    step("Clear table", clear_ok, clear_detail)
    if not clear_ok:
        step_skip("Insert data", "Skipped (clear failed)")
        section_result(False)
        return False

    # --- Step 4: Insert ---
    location_groups_data = data['data'].get('location_groups', {})
    rows = []
    for label, sub_locations in location_groups_data.items():
        rows.append({
            "label": label,
            "value": json.dumps(sub_locations, ensure_ascii=False)
        })

    try:
        insert_url = "https://api.botpress.cloud/v1/tables/FilterLocationsTable/rows"
        res = requests.post(
            insert_url,
            headers=HEADERS,
            json={"rows": rows},
            timeout=BOTPRESS_TIMEOUT
        )
        res.raise_for_status()
        step("Insert data", True, f"{len(rows)} rows inserted")
        section_result(True)
        return True
    except requests.exceptions.HTTPError as e:
        step("Insert data", False, f"HTTP {e.response.status_code}: {e.response.text}")
        section_result(False)
        return False
    except Exception as e:
        step("Insert data", False, str(e))
        section_result(False)
        return False


# === ENTRY POINT ===

if __name__ == "__main__":
    start_time = datetime.now()

    line()
    print(f"   IRRES -> Botpress Sync")
    print(f"   {start_time.strftime('%Y-%m-%d  %H:%M:%S')}")
    line()

    results = {
        "LISTINGS":      sync_listings(),
        "OFFICE IMAGES": sync_office_images(),
        "LOCATIONS":     sync_locations(),
    }

    duration = datetime.now() - start_time
    total    = len(results)
    passed   = sum(1 for ok in results.values() if ok)
    failed   = [name for name, ok in results.items() if not ok]

    # Final summary
    if passed == total:
        overall = f"✅  {passed}/{total} succeeded"
    elif passed == 0:
        overall = f"❌  0/{total} succeeded"
    else:
        overall = f"⚠️   {passed}/{total} succeeded"

    minutes, seconds = divmod(int(duration.total_seconds()), 60)
    duration_str = f"{minutes}m {seconds:02d}s"

    print(f"\nSYNC COMPLETE  {overall}  —  Duration: {duration_str}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    line()

    if passed < total:
        sys.exit(1)
