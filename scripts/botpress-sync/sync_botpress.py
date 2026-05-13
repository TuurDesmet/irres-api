import requests
import json
import os
import sys
import time
from datetime import datetime

# === CONFIGURATION ===
BOT_ID = os.getenv("BOT_ID")
TOKEN = os.getenv("BOTPRESS_TOKEN")
IRRES_API_KEY = os.getenv("IRRES_API_KEY") or os.getenv("API_KEY")

BASE_API       = "https://irres-api.onrender.com/api"
HEALTH_URL     = "https://irres-api.onrender.com/health"
LISTINGS_API   = f"{BASE_API}/listings"
IMAGES_API     = f"{BASE_API}/office-images"
LOCATIONS_API  = f"{BASE_API}/locations"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "x-bot-id": BOT_ID,
    "Content-Type": "application/json"
}

IRRES_API_HEADERS = {"X-API-KEY": IRRES_API_KEY} if IRRES_API_KEY else {}

# === TIMEOUT CONFIGURATION ===
# (connect_seconds, read_seconds)
LISTINGS_TIMEOUT  = (15, 450)  # /api/listings visits every detail page — slow by design
FAST_API_TIMEOUT  = (15, 90)   # /api/office-images and /api/locations are fast
BOTPRESS_TIMEOUT  = (10, 30)   # Botpress Cloud is always fast
HEALTH_TIMEOUT    = (10, 15)   # Quick ping to wake up the server

# === WAKE-UP & RETRY CONFIGURATION ===
WAKE_UP_MAX_WAIT    = 90   # Max seconds to wait for the server to come online
WAKE_UP_INTERVAL    = 5    # Seconds between each health check ping
LISTINGS_MAX_RETRY  = 3    # Number of times to retry /api/listings on failure
LISTINGS_RETRY_WAIT = 30   # Seconds to wait between each retry

# === DISPLAY HELPERS ===

W = 50  # Line width for separators

def line():
    print("=" * W)

def divider():
    print("-" * W)

def step(label, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {label:<22} {status}  {detail}")

def step_warn(label, detail=""):
    print(f"  {label:<22} ⚠️   {detail}")

def step_skip(label, reason="Skipped"):
    print(f"  {label:<22} ⏭️   {reason}")

def section_header(title):
    print(f"\n{title}")

def section_result(ok):
    status = "✅  SUCCESS" if ok else "❌  FAILED"
    divider()
    print(f"  {'Result':<22} {status}")
    line()


# === WAKE-UP HELPER ===

def wait_for_server():
    """
    Pings the /health endpoint repeatedly until the server responds with HTTP 200.

    Render.com free-tier servers spin down after inactivity and need 30-50
    seconds to cold-start. Sending a lightweight /health ping first wakes the
    server up without triggering the expensive /api/listings scrape.

    Returns:
        (True, elapsed_seconds)  if server came online within WAKE_UP_MAX_WAIT
        (False, elapsed_seconds) if server did not respond in time
    """
    start = time.time()
    attempt = 0

    while True:
        elapsed = int(time.time() - start)
        attempt += 1

        try:
            res = requests.get(HEALTH_URL, headers=IRRES_API_HEADERS, timeout=HEALTH_TIMEOUT)
            if res.status_code == 200:
                return True, elapsed
        except Exception:
            pass  # Server not yet awake — keep trying

        if elapsed >= WAKE_UP_MAX_WAIT:
            return False, elapsed

        time.sleep(WAKE_UP_INTERVAL)


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
    if not isinstance(images, list):
        return False, "No 'data' list found in response."
    valid_rows = [
        x for x in images
        if isinstance(x, dict) and isinstance(x.get('image_url'), str) and x['image_url'].strip()
    ]
    if len(valid_rows) == 0:
        return False, "All image entries are empty or missing URLs."
    return True, f"{len(valid_rows)} images found"


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
    Syncs listings to ListingsTable.

    Flow:
      1. Wake up the Render.com server via /health ping.
      2. Fetch /api/listings with automatic retry on failure.
      3. Validate the response.
      4. Clear ListingsTable.
      5. Insert fresh rows.

    Returns True if fully successful, False otherwise.
    """
    section_header("LISTINGS")

    # --- Check API key ---
    if not IRRES_API_KEY:
        step("Wake up server", False, "IRRES_API_KEY is not set")
        step_skip("Fetch API")
        step_skip("Validate data")
        step_skip("Clear table")
        step_skip("Insert data")
        section_result(False)
        return False

    # --- Step 1: Wake up server ---
    # Render.com free tier spins down after inactivity.
    # We ping /health repeatedly until the server responds, BEFORE hitting
    # the expensive /api/listings endpoint.
    print(f"  {'Wake up server':<22} ⏳  Pinging server (max {WAKE_UP_MAX_WAIT}s)...")
    server_ok, elapsed = wait_for_server()
    step("Wake up server", server_ok, f"Server online in {elapsed}s" if server_ok else f"No response after {elapsed}s")

    if not server_ok:
        step_skip("Fetch API",     "Skipped (server offline)")
        step_skip("Validate data", "Skipped (server offline)")
        step_skip("Clear table",   "Skipped (server offline)")
        step_skip("Insert data",   "Skipped (server offline)")
        section_result(False)
        return False

    # --- Step 2: Fetch with retry ---
    # /api/listings scrapes every detail page one by one — it can still fail
    # on the first attempt if the server just woke up and isn't fully ready.
    # We retry up to LISTINGS_MAX_RETRY times with a wait between each attempt.
    data = None
    fetch_error = None

    for attempt in range(1, LISTINGS_MAX_RETRY + 1):
        try:
            res = requests.get(LISTINGS_API, headers=IRRES_API_HEADERS, timeout=LISTINGS_TIMEOUT)
            res.raise_for_status()
            data = res.json()
            fetch_error = None
            break  # Success — stop retrying
        except Exception as e:
            fetch_error = str(e)
            if attempt < LISTINGS_MAX_RETRY:
                step_warn(
                    "Fetch API",
                    f"Attempt {attempt}/{LISTINGS_MAX_RETRY} failed — retrying in {LISTINGS_RETRY_WAIT}s"
                )
                time.sleep(LISTINGS_RETRY_WAIT)

    if data is None:
        step("Fetch API", False, f"All {LISTINGS_MAX_RETRY} attempts failed: {fetch_error}")
        step_skip("Validate data", "Skipped (fetch failed)")
        step_skip("Clear table",   "Skipped (fetch failed)")
        step_skip("Insert data",   "Skipped (fetch failed)")
        section_result(False)
        return False

    attempt_label = f"{len(data.get('listings', []))} listings fetched"
    if attempt > 1:
        attempt_label += f" (attempt {attempt}/{LISTINGS_MAX_RETRY})"
    step("Fetch API", True, attempt_label)

    # --- Step 3: Validate ---
    is_valid, reason = validate_listings_data(data)
    step("Validate data", is_valid, reason)
    if not is_valid:
        step_skip("Clear table", "Skipped (validation failed)")
        step_skip("Insert data", "Skipped (validation failed)")
        section_result(False)
        return False

    # --- Step 4: Clear table ---
    clear_ok, clear_detail = delete_table_rows("ListingsTable")
    step("Clear table", clear_ok, clear_detail)
    if not clear_ok:
        step_skip("Insert data", "Skipped (clear failed)")
        section_result(False)
        return False

    # --- Step 5: Insert ---
    rows = []
    for l in data['listings']:
        details_json = json.dumps(l.get('details', {}), ensure_ascii=False)
        rows.append({
            "listing_id":     l.get('listing_id'),
            "listing_url":    l.get('listing_url'),
            "photo_url":      l.get('photo_url'),
            "price":          l.get('price'),
            "location":       l.get('location'),
            "description":    l.get('description'),
            "listing_type":   l.get('listing_type'),
            "Title":          l.get('title', ""),
            "Button1_Label":  l.get('button1_label', "Bekijk het op onze website"),
            "Button1_Value":  l.get('button1_value', ""),
            "Button2_Label":  l.get('button2_label', ""),
            "Button2_email": l.get('button2_value', ""),
            "Button3_Label":  l.get('button3_label'),
            "Button3_Value":  l.get('button3_value'),
            "details":        details_json,
            "last_updated":   datetime.now().isoformat()
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
    Syncs office images to OfficeImagesTable.
    Server is already awake after sync_listings() so no wake-up needed.
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
        fetch_detail = f"{len(data.get('data') or [])} images fetched"
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
    for item in data['data']:
        if not isinstance(item, dict):
            continue
        url = item.get('image_url') or ''
        if not isinstance(url, str) or not url.strip():
            continue
        image_rows.append({
            "office_name": item.get('office_name') or '',
            "image_url":   url.strip(),
        })

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
    Syncs locations to FilterLocationsTable.
    Each location group gets its own row:
      label (string) - display name,  e.g. "Gent + deelgemeenten"
      value (string) - JSON array,    e.g. '["Gent", "Mariakerke", ...]'

    Server is already awake after sync_listings() so no wake-up needed.
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

    minutes, seconds = divmod(int(duration.total_seconds()), 60)
    duration_str = f"{minutes}m {seconds:02d}s"

    if passed == total:
        overall = f"✅  {passed}/{total} succeeded"
    elif passed == 0:
        overall = f"❌  0/{total} succeeded"
    else:
        overall = f"⚠️   {passed}/{total} succeeded"

    print(f"\nSYNC COMPLETE  {overall}  —  Duration: {duration_str}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    line()

    if passed < total:
        sys.exit(1)
