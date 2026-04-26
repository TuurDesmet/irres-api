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
    return True, f"{len(listings)} valid listing(s) found."


def validate_office_images_data(data):
    if not isinstance(data, dict):
        return False, "Response is not a JSON object."
    images = data.get('data')
    if not images or not isinstance(images, dict):
        return False, "No 'data' dict found in response."
    valid_entries = {k: v for k, v in images.items() if isinstance(v, str) and v.strip()}
    if len(valid_entries) == 0:
        return False, "All image entries are empty or missing URLs."
    return True, f"{len(valid_entries)} valid office image(s) found."


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
    return True, f"{len(all_locations)} location(s) across {len(location_groups)} group(s) found."


# === BOTPRESS TABLE HELPERS ===

def delete_table_rows(table_name):
    """
    Deletes all rows from a Botpress table.
    Only called after successful data validation so we never wipe a table
    and then fail to repopulate it.
    """
    print(f"  Clearing table '{table_name}'...")
    url = f"https://api.botpress.cloud/v1/tables/{table_name}/rows/delete"
    try:
        res = requests.post(
            url,
            headers=HEADERS,
            json={"deleteAllRows": True},
            timeout=BOTPRESS_TIMEOUT
        )
        res.raise_for_status()
        print(f"  Table '{table_name}' cleared successfully.")
    except requests.exceptions.HTTPError as err:
        print(f"  Warning: Could not clear table '{table_name}': {err}")
        print(f"  Response body: {err.response.text}")
    except Exception as e:
        print(f"  Error clearing table '{table_name}': {e}")
        raise


# === SYNC FUNCTIONS ===

def sync_listings():
    """
    Fetches all listings from the IRRES API and inserts them into ListingsTable.
    Aborts without touching Botpress if the API response fails validation.
    """
    print("\n[Listings] Fetching data from API...")
    if not IRRES_API_KEY:
        print("[Listings] ERROR: IRRES_API_KEY / API_KEY is not set.")
        print("[Listings] Botpress table was NOT modified.")
        return
    try:
        res = requests.get(LISTINGS_API, headers=IRRES_API_HEADERS, timeout=LISTINGS_TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"[Listings] ERROR: Failed to fetch listings: {e}")
        print("[Listings] Botpress table was NOT modified.")
        return

    is_valid, reason = validate_listings_data(data)
    if not is_valid:
        print(f"[Listings] ERROR: Validation failed - {reason}")
        print("[Listings] Botpress table was NOT modified to prevent data loss.")
        return
    print(f"[Listings] OK: Validation passed - {reason}")

    delete_table_rows("ListingsTable")

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
        print(f"[Listings] OK: Inserted {len(rows)} listing(s) successfully.")
    except requests.exceptions.HTTPError as e:
        print(f"[Listings] ERROR: Failed to insert listings: {e}")
        print(f"[Listings] ERROR: Response body: {e.response.text}")
    except Exception as e:
        print(f"[Listings] ERROR: Failed to insert listings: {e}")


def sync_office_images():
    """
    Fetches office images from the IRRES API and inserts them into OfficeImagesTable.
    Aborts without touching Botpress if the API response fails validation.
    """
    print("\n[OfficeImages] Fetching data from API...")
    if not IRRES_API_KEY:
        print("[OfficeImages] ERROR: IRRES_API_KEY / API_KEY is not set.")
        print("[OfficeImages] Botpress table was NOT modified.")
        return
    try:
        res = requests.get(IMAGES_API, headers=IRRES_API_HEADERS, timeout=FAST_API_TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"[OfficeImages] ERROR: Failed to fetch office images: {e}")
        print("[OfficeImages] Botpress table was NOT modified.")
        return

    is_valid, reason = validate_office_images_data(data)
    if not is_valid:
        print(f"[OfficeImages] ERROR: Validation failed - {reason}")
        print("[OfficeImages] Botpress table was NOT modified to prevent data loss.")
        return
    print(f"[OfficeImages] OK: Validation passed - {reason}")

    delete_table_rows("OfficeImagesTable")

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
        print(f"[OfficeImages] OK: Inserted {len(image_rows)} office image(s) successfully.")
    except requests.exceptions.HTTPError as e:
        print(f"[OfficeImages] ERROR: Failed to insert office images: {e}")
        print(f"[OfficeImages] ERROR: Response body: {e.response.text}")
    except Exception as e:
        print(f"[OfficeImages] ERROR: Failed to insert office images: {e}")


def sync_locations():
    """
    Fetches locations from the IRRES API and stores them in FilterLocationsTable
    as a SINGLE ROW with two columns:

      all_locations   (string) - JSON array of all location labels
                                 e.g. '[{"label":"Gent","value":"Gent"}, ...]'

      location_groups (string) - JSON object mapping label to sub-location array
                                 e.g. '{"Gent + deelgemeenten":["Gent","Mariakerke",...]}'

    One row is used so the chatbot can load the full dropdown dataset in a
    single table read without pagination.

    Aborts without touching Botpress if the API response fails validation.
    """
    print("\n[Locations] Fetching data from API...")
    if not IRRES_API_KEY:
        print("[Locations] ERROR: IRRES_API_KEY / API_KEY is not set.")
        print("[Locations] Botpress table was NOT modified.")
        return

    try:
        res = requests.get(LOCATIONS_API, headers=IRRES_API_HEADERS, timeout=FAST_API_TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"[Locations] ERROR: Failed to fetch locations: {e}")
        print("[Locations] Botpress table was NOT modified.")
        return

    # --- Validate before touching Botpress ---
    is_valid, reason = validate_locations_data(data)
    if not is_valid:
        print(f"[Locations] ERROR: Validation failed - {reason}")
        print("[Locations] Botpress table was NOT modified to prevent data loss.")
        return
    print(f"[Locations] OK: Validation passed - {reason}")

    # --- Build the payload ---
    all_locations_data   = data['data'].get('all_locations', [])
    location_groups_data = data['data'].get('location_groups', {})

    all_locations_json   = json.dumps(all_locations_data,   ensure_ascii=False)
    location_groups_json = json.dumps(location_groups_data, ensure_ascii=False)

    row_payload = {
        "all_locations":   all_locations_json,
        "location_groups": location_groups_json
    }

    # --- Debug preview so column mismatches are visible in the Actions log ---
    print(f"[Locations] Payload columns        : {list(row_payload.keys())}")
    print(f"[Locations] all_locations length   : {len(all_locations_json)} chars "
          f"({len(all_locations_data)} items)")
    print(f"[Locations] location_groups length : {len(location_groups_json)} chars "
          f"({len(location_groups_data)} groups)")
    print(f"[Locations] First 3 locations      : {all_locations_data[:3]}")
    print(f"[Locations] First location group   : "
          f"{next(iter(location_groups_data.items()), ('none', []))}")

    # --- Clear old data and insert the single fresh row ---
    delete_table_rows("FilterLocationsTable")

    try:
        insert_url = "https://api.botpress.cloud/v1/tables/FilterLocationsTable/rows"
        print(f"[Locations] POST {insert_url}")
        res = requests.post(
            insert_url,
            headers=HEADERS,
            json={"rows": [row_payload]},
            timeout=BOTPRESS_TIMEOUT
        )
        res.raise_for_status()
        print("[Locations] OK: Inserted 1 row with all locations and location groups.")
    except requests.exceptions.HTTPError as e:
        print(f"[Locations] ERROR: Failed to insert locations: {e}")
        print(f"[Locations] ERROR: Status code  : {e.response.status_code}")
        print(f"[Locations] ERROR: Response body: {e.response.text}")
        print(f"[Locations] ERROR: Request URL  : {e.response.url}")
    except Exception as e:
        print(f"[Locations] ERROR: Failed to insert locations: {e}")


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
        print(f"\nERROR: Unexpected error during sync: {e}")
        overall_success = False
        sys.exit(1)

    if overall_success:
        print("\n" + "=" * 50)
        print("OK: Sync process completed.")
        print("=" * 50)
