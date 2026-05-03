import requests
import json
import os
import sys
import time
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Any, Optional

# =============================================================================
# BLOCK 1: CONFIGURATION & ENVIRONMENT
# =============================================================================

# Botpress Credentials
BOT_ID = os.getenv("BOT_ID")
TOKEN = os.getenv("BOTPRESS_TOKEN")

# IRRES API Credentials
# Note: Render.com deployments often use 'API_KEY' or 'IRRES_API_KEY'
IRRES_API_KEY = os.getenv("IRRES_API_KEY") or os.getenv("API_KEY")

# Endpoint Definitions
BASE_API       = "https://irres-api.onrender.com/api"
HEALTH_URL     = "https://irres-api.onrender.com/health"
LISTINGS_API   = f"{BASE_API}/listings"
IMAGES_API     = f"{BASE_API}/office-images"
LOCATIONS_API  = f"{BASE_API}/locations"

# Authentication Headers
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "x-bot-id": BOT_ID,
    "Content-Type": "application/json"
}

IRRES_API_HEADERS = {"X-API-KEY": IRRES_API_KEY} if IRRES_API_KEY else {}

# Table Name Definitions
TABLE_LISTINGS  = "ListingsTable"
TABLE_IMAGES    = "OfficeImagesTable"
TABLE_LOCATIONS = "FilterLocationsTable"

# =============================================================================
# BLOCK 2: TIMEOUT & RETRY SETTINGS
# =============================================================================

# Timeouts: (Connection Timeout, Read Timeout) in seconds
# /api/listings is slow as it visits every detail page (3-8 minutes expected)
TIMEOUT_LISTINGS = (20, 480)
TIMEOUT_FAST     = (15, 90)
TIMEOUT_BOTPRESS = (10, 30)
TIMEOUT_HEALTH   = (10, 15)

# Wait/Retry configuration for Render.com free tier
WAKE_UP_MAX_WAIT    = 120   # Seconds to wait for server to wake up
WAKE_UP_INTERVAL    = 5     # Delay between health pings
LISTINGS_MAX_RETRY  = 3     # Retry count for the scraping endpoint
LISTINGS_RETRY_WAIT = 30    # Delay between retries on 5xx errors

# =============================================================================
# BLOCK 3: LOGGING & UI HELPERS
# =============================================================================

# Configure system logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SyncEngine")

# Console display width
W = 60

def line():
    """Prints a double horizontal separator."""
    print("=" * W)

def divider():
    """Prints a single horizontal separator."""
    print("-" * W)

def step(label: str, ok: bool, detail: str = ""):
    """
    Prints a status line for a specific sync step.
    
    Args:
        label: The name of the step.
        ok: Whether the step succeeded.
        detail: Additional information or error message.
    """
    status = "✅" if ok else "❌"
    print(f"  {label:<25} {status}  {detail}")

def step_warn(label: str, detail: str = ""):
    """Prints a warning status line."""
    print(f"  {label:<25} ⚠️   {detail}")

def step_skip(label: str, reason: str = "Skipped"):
    """Prints a skipped status line."""
    print(f"  {label:<25} ⏭️   {reason}")

def section_header(title: str):
    """Prints a formatted header for a sync section."""
    print(f"\n[ {title} ]")
    divider()

def section_result(ok: bool):
    """Prints the final result status for a section."""
    status = "SUCCESS" if ok else "FAILED"
    icon = "✅" if ok else "❌"
    divider()
    print(f"  {'Overall Result':<25} {icon}  {status}")
    line()

# =============================================================================
# BLOCK 4: PRE-FLIGHT CHECKS
# =============================================================================

def run_preflight_checks() -> bool:
    """
    Verifies that all required environment variables are set.
    
    Returns:
        True if all checks pass, False otherwise.
    """
    section_header("PRE-FLIGHT CHECKS")
    checks = {
        "BOT_ID": bool(BOT_ID),
        "BOTPRESS_TOKEN": bool(TOKEN),
        "IRRES_API_KEY": bool(IRRES_API_KEY)
    }
    
    all_pass = True
    for var, exists in checks.items():
        step(f"Check {var}", exists, "Found" if exists else "MISSING")
        if not exists:
            all_pass = False
            
    section_result(all_pass)
    return all_pass

# =============================================================================
# BLOCK 5: SERVER MANAGEMENT
# =============================================================================

def wait_for_server() -> Tuple[bool, int]:
    """
    Pings the /health endpoint repeatedly to wake up the Render.com server.
    
    Free tier Render instances spin down after inactivity. This function
    ensures the server is 'warm' before we send an expensive scraping request.
    
    Returns:
        (Success Boolean, Elapsed Seconds)
    """
    start = time.time()
    
    while True:
        elapsed = int(time.time() - start)
        
        try:
            res = requests.get(
                HEALTH_URL, 
                headers=IRRES_API_HEADERS, 
                timeout=TIMEOUT_HEALTH
            )
            if res.status_code == 200:
                return True, elapsed
        except Exception:
            pass  # Expected if server is booting
            
        if elapsed >= WAKE_UP_MAX_WAIT:
            return False, elapsed
            
        time.sleep(WAKE_UP_INTERVAL)

# =============================================================================
# BLOCK 6: VALIDATION LOGIC
# =============================================================================

def validate_listings_payload(data: Any) -> Tuple[bool, str]:
    """
    Validates the structure of the listings API response.
    
    Args:
        data: The decoded JSON response.
        
    Returns:
        (IsValid, ErrorMessage/SuccessMessage)
    """
    if not isinstance(data, dict):
        return False, "Payload is not a JSON object"
        
    if not data.get('success'):
        return False, f"API success flag is False: {data.get('error', 'No msg')}"
        
    listings = data.get('listings')
    if not isinstance(listings, list):
        return False, "Field 'listings' is missing or not an array"
        
    if not listings:
        return False, "Listings array is empty"
        
    # Check first item for essential keys
    sample = listings[0]
    required = ['listing_id', 'listing_url']
    for key in required:
        if key not in sample:
            return False, f"Missing key '{key}' in listing data"
            
    return True, f"Found {len(listings)} valid entries"

def validate_image_payload(data: Any) -> Tuple[bool, str]:
    """Validates the office images API response."""
    if not isinstance(data, dict) or 'data' not in data:
        return False, "Missing 'data' wrapper"
    return True, "Valid image map"

def validate_location_payload(data: Any) -> Tuple[bool, str]:
    """Validates the locations API response."""
    if not isinstance(data, dict) or 'data' not in data:
        return False, "Missing 'data' wrapper"
    inner = data.get('data')
    if 'all_locations' not in inner or 'location_groups' not in inner:
        return False, "Missing location data structures"
    return True, "Valid location structure"

# =============================================================================
# BLOCK 7: BOTPRESS TABLE OPERATIONS
# =============================================================================

def clear_botpress_table(table_name: str) -> Tuple[bool, str]:
    """
    Deletes all existing rows from a specific Botpress table.
    
    Args:
        table_name: The table to empty.
        
    Returns:
        (Success, Detail)
    """
    url = f"https://api.botpress.cloud/v1/tables/{table_name}/rows/delete"
    try:
        res = requests.post(
            url,
            headers=HEADERS,
            json={"deleteAllRows": True},
            timeout=TIMEOUT_BOTPRESS
        )
        res.raise_for_status()
        return True, "Table cleared"
    except requests.exceptions.HTTPError as err:
        return False, f"HTTP {err.response.status_code}: {err.response.text}"
    except Exception as e:
        return False, str(e)

def insert_botpress_rows(table_name: str, rows: List[Dict]) -> Tuple[bool, str]:
    """
    Uploads a batch of rows to a Botpress table.
    
    Args:
        table_name: Target table.
        rows: List of dictionary records.
        
    Returns:
        (Success, Detail)
    """
    url = f"https://api.botpress.cloud/v1/tables/{table_name}/rows"
    try:
        # Botpress handles batching internally if the list is reasonably sized
        res = requests.post(
            url,
            headers=HEADERS,
            json={"rows": rows},
            timeout=TIMEOUT_BOTPRESS
        )
        res.raise_for_status()
        return True, f"{len(rows)} records inserted"
    except requests.exceptions.HTTPError as err:
        return False, f"HTTP {err.response.status_code}: {err.response.text}"
    except Exception as e:
        return False, str(e)

# =============================================================================
# BLOCK 8: CORE SYNCHRONIZATION LOGIC
# =============================================================================

def sync_listings() -> bool:
    """
    Executes the full listing synchronization pipeline.
    
    Steps:
    1. Wake up server
    2. Fetch /api/listings (with retry)
    3. Validate
    4. Transform data (adding address and page_content)
    5. Clear table
    6. Insert new data
    
    Returns:
        True if the entire section succeeded.
    """
    section_header("SYNC: PROPERTY LISTINGS")
    
    # 1. Wake up
    print(f"  {'Wake up server':<25} ⏳  Pinging...")
    online, seconds = wait_for_server()
    step("Wake up server", online, f"Online in {seconds}s" if online else "TIMEOUT")
    if not online: 
        section_result(False)
        return False

    # 2. Fetch Data
    api_data = None
    fetch_error = ""
    for attempt in range(1, LISTINGS_MAX_RETRY + 1):
        try:
            res = requests.get(
                LISTINGS_API, 
                headers=IRRES_API_HEADERS, 
                timeout=TIMEOUT_LISTINGS
            )
            res.raise_for_status()
            api_data = res.json()
            break
        except Exception as e:
            fetch_error = str(e)
            if attempt < LISTINGS_MAX_RETRY:
                step_warn("Fetch API", f"Attempt {attempt} failed, retrying...")
                time.sleep(LISTINGS_RETRY_WAIT)

    if not api_data:
        step("Fetch API", False, f"Failed after {LISTINGS_MAX_RETRY} attempts: {fetch_error}")
        section_result(False)
        return False
    
    step("Fetch API", True, f"Received {len(api_data.get('listings', []))} items")

    # 3. Validate
    valid, msg = validate_listings_payload(api_data)
    step("Validate data", valid, msg)
    if not valid:
        section_result(False)
        return False

    # 4. Transform & Build Rows
    # This block maps API keys to Botpress table columns
    rows = []
    timestamp = datetime.now().isoformat()
    
    for item in api_data['listings']:
        rows.append({
            "listing_id":    item.get("listing_id", ""),
            "listing_url":   item.get("listing_url", ""),
            "photo_url":     item.get("photo_url", ""),
            "price":         item.get("price", ""),
            "location":      item.get("location", ""),
            "description":   item.get("description", ""),
            "listing_type":  item.get("listing_type", ""),
            "address":       item.get("address", ""),       # Syncing new field
            "page_content":  item.get("page_content", ""),  # Syncing new field
            "Title":         item.get("Title", ""),
            "Button1_Label": item.get("Button1_Label", "Bekijk het op onze website"),
            "Button2_Label": item.get("Button2_Label", ""),
            "Button2_email": item.get("Button2_email", ""),
            "Button3_Label": item.get("Button3_Label", ""),
            "Button3_Value": item.get("Button3_Value", ""),
            "last_updated":  timestamp
        })
    step("Transform data", True, "Mapping complete")

    # 5. Clear Table
    cleared, c_msg = clear_botpress_table(TABLE_LISTINGS)
    step("Clear table", cleared, c_msg)
    if not cleared:
        section_result(False)
        return False

    # 6. Insert Data
    inserted, i_msg = insert_botpress_rows(TABLE_LISTINGS, rows)
    step("Insert data", inserted, i_msg)
    
    section_result(inserted)
    return inserted

def sync_office_images() -> bool:
    """Synchronizes office building photos to OfficeImagesTable."""
    section_header("SYNC: OFFICE IMAGES")
    
    try:
        res = requests.get(IMAGES_API, headers=IRRES_API_HEADERS, timeout=TIMEOUT_FAST)
        res.raise_for_status()
        data = res.json()
        
        valid, msg = validate_image_payload(data)
        step("Fetch & Validate", valid, msg)
        if not valid: return False
        
        # Mapping to table format: {office_name, image_url}
        image_map = data['data']
        rows = []
        for key, url in image_map.items():
            if url:
                # Clean name: IrresGentImage -> Gent
                name = key.replace("Irres", "").replace("Image", "")
                rows.append({"office_name": name, "image_url": url})
        
        clear_botpress_table(TABLE_IMAGES)
        ok, msg = insert_botpress_rows(TABLE_IMAGES, rows)
        step("Insert data", ok, msg)
        
        section_result(ok)
        return ok
    except Exception as e:
        step("Sync Error", False, str(e))
        section_result(False)
        return False

def sync_locations() -> bool:
    """Synchronizes city filter groups to FilterLocationsTable."""
    section_header("SYNC: SEARCH LOCATIONS")
    
    try:
        res = requests.get(LOCATIONS_API, headers=IRRES_API_HEADERS, timeout=TIMEOUT_FAST)
        res.raise_for_status()
        data = res.json()
        
        valid, msg = validate_location_payload(data)
        step("Fetch & Validate", valid, msg)
        if not valid: return False
        
        # Groups are stored as JSON strings in the 'value' column for Botpress parsing
        groups = data['data']['location_groups']
        rows = [{"label": k, "value": json.dumps(v)} for k, v in groups.items()]
        
        clear_botpress_table(TABLE_LOCATIONS)
        ok, msg = insert_botpress_rows(TABLE_LOCATIONS, rows)
        step("Insert data", ok, msg)
        
        section_result(ok)
        return ok
    except Exception as e:
        step("Sync Error", False, str(e))
        section_result(False)
        return False

# =============================================================================
# BLOCK 9: MAIN EXECUTION FLOW
# =============================================================================

def run_orchestrator():
    """Main entrance point for the sync script."""
    start_time = datetime.now()
    
    line()
    print(f"   IRRES -> BOTPRESS SYNC ENGINE v2.0")
    print(f"   Session started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    line()

    # Phase 1: Verification
    if not run_preflight_checks():
        print("\nCRITICAL: Pre-flight checks failed. Aborting sync.")
        sys.exit(1)

    # Phase 2: Synchronization
    # We store results in a dictionary for a final summary report
    results = {
        "Listings":  sync_listings(),
        "Images":    sync_office_images(),
        "Locations": sync_locations()
    }

    # Phase 3: Final Reporting
    end_time = datetime.now()
    duration = end_time - start_time
    minutes, seconds = divmod(int(duration.total_seconds()), 60)
    
    print("\n[ FINAL SUMMARY REPORT ]")
    divider()
    
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    
    for module, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {module:<25} {icon} {'Success' if ok else 'Failed'}")
    
    divider()
    print(f"  {'Total Modules':<25} {total}")
    print(f"  {'Passed':<25} {passed}")
    print(f"  {'Failed':<25} {total - passed}")
    print(f"  {'Total Duration':<25} {minutes}m {seconds:02d}s")
    line()
    
    # Exit with appropriate status code for GitHub Actions
    if passed < total:
        print("\nWARNING: One or more sync modules failed.")
        sys.exit(1)
    else:
        print("\nSUCCESS: All data synchronized correctly.")
        sys.exit(0)

if __name__ == "__main__":
    try:
        run_orchestrator()
    except KeyboardInterrupt:
        print("\n\nProcess interrupted by user. Exiting...")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Unhandled exception in main: {str(e)}", exc_info=True)
        sys.exit(1)
