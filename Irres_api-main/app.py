# =============================================================================
# ===== Irres-API ======
# app.py
# IRRES.be Scraper API
# Combines Listings, Locations, and Office Image scraping in one application.
#
# STRUCTURE OVERVIEW:
#   BLOCK 1  — Imports & App Initialization
#   BLOCK 2  — Rate Limiting Configuration
#   BLOCK 3  — Security & Authentication
#   BLOCK 4  — Logging & Global Constants
#   BLOCK 5  — Utility: secure_get() & Type Mapping
#   BLOCK 6  — Class: IRRESLocationScraper
#              6a  fetch_page()
#              6b  parse_locations()         ← FIXED (targets .search-data)
#              6c  scrape()
#   BLOCK 7  — Class: IRRESOfficeImagesScraper
#              7a  fetch_page()
#              7b  extract_image_url_from_section()
#              7c  parse_office_images()
#              7d  scrape()
#   BLOCK 8  — Listing Helper: normalize_text()
#   BLOCK 9  — Listing Helper: normalize_url()
#   BLOCK 10 — Listing Helper: extract_listing_id_from_url()
#   BLOCK 11 — Listing Helper: format_price_string()
#   BLOCK 12 — Listing Helper: format_details_as_string()
#   BLOCK 13 — Listing Helper: parse_main_listing_card()
#   BLOCK 14 — Listing Helper: find_photo_on_element()
#   BLOCK 15 — Listing Helper: find_landscape_image_from_detail()
#   BLOCK 16 — Listing Helper: extract_property_details_from_detail_soup()
#   BLOCK 17 — Listing Helper: extract_contact_and_email_from_detail()
#   BLOCK 18 — Listing Helper: fetch_detail_page()
#   BLOCK 19 — API Endpoint: /api/listings
#   BLOCK 20 — API Endpoint: /api/locations
#   BLOCK 21 — API Endpoint: /api/office-images
#   BLOCK 22 — API Endpoint: /health & / (root)
#   BLOCK 23 — Run Server
# =============================================================================


# =============================================================================
# BLOCK 1 — IMPORTS & APP INITIALIZATION
# =============================================================================

import os
import re
import html
import time
import json
import logging
import unicodedata
from datetime import datetime

from flask import Flask, jsonify, Response, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import requests
from bs4 import BeautifulSoup

# Initialize Flask App
app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False  # Ensure UTF-8 characters are preserved in JSON output


# =============================================================================
# BLOCK 2 — RATE LIMITING CONFIGURATION
# =============================================================================
#
# Limits expensive scraping endpoints to prevent:
#   - Server resource exhaustion (CPU, memory, bandwidth)
#   - Worker process starvation (only 2 sync workers on Render.com)
#   - IRRES.be IP ban from hitting their server too frequently
#   - Authenticated DoS attacks on expensive endpoints
#
# Storage: in-memory (suitable for single-server deployments on Render.com)
# Global defaults apply to ALL endpoints unless overridden by @limiter.limit()
# =============================================================================

limiter = Limiter(
    app=app,
    key_func=get_remote_address,          # Rate limit per IP address
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"               # In-memory storage (single server)
)

# Exempt low-cost endpoints from rate limiting
limiter.exempt(lambda: request.endpoint == 'health')
limiter.exempt(lambda: request.endpoint == 'index')


# =============================================================================
# BLOCK 3 — SECURITY & AUTHENTICATION
# =============================================================================
#
# API Key Authentication via X-API-KEY header ONLY.
# Query parameters (?api_key=...) are explicitly REJECTED.
#
# This prevents key exposure in:
#   - Server / proxy / CDN access logs (which log full URLs)
#   - Browser history
#   - HTTP Referer headers
#   - Log aggregation systems
#
# Compliance: OWASP Top 10 2021 — A02:2021 Cryptographic Failures
# =============================================================================

# Load API key from environment — raises at startup if missing
API_KEY = os.getenv("API_KEY")
if API_KEY is None:
    raise ValueError("API_KEY environment variable is required but not set")


@app.before_request
def require_api_key():
    """
    Authenticate every incoming request using the X-API-KEY header.

    Rules:
      1. Static file endpoints are exempt.
      2. Any request with ?api_key=... in the query string is rejected (401).
      3. Missing X-API-KEY header → 401 Unauthorized.
      4. Wrong X-API-KEY value   → 401 Unauthorized.
      5. Correct header          → request proceeds normally.
    """
    # Allow static files through without authentication
    if request.endpoint == 'static':
        return

    # Explicitly block query-parameter authentication attempts
    if 'api_key' in request.args:
        logger.warning(
            f"SECURITY: Rejected ?api_key query param from {request.remote_addr} "
            f"on {request.path}. Use X-API-KEY header instead."
        )
        return jsonify({
            "error": "Unauthorized",
            "message": "API key must be provided via X-API-KEY header, not query parameters"
        }), 401

    # Extract key from header
    provided_api_key = request.headers.get('X-API-KEY')

    if not provided_api_key:
        logger.warning(
            f"SECURITY: Missing X-API-KEY header from {request.remote_addr} "
            f"on {request.path}"
        )
        return jsonify({
            "error": "Unauthorized",
            "message": "X-API-KEY header is required"
        }), 401

    if provided_api_key != API_KEY:
        logger.warning(
            f"SECURITY: Invalid X-API-KEY from {request.remote_addr} "
            f"on {request.path}"
        )
        return jsonify({
            "error": "Unauthorized",
            "message": "Invalid X-API-KEY"
        }), 401

    # Authentication successful — let the request through
    return None


@app.errorhandler(429)
def ratelimit_handler(e):
    """
    Handle HTTP 429 Too Many Requests errors produced by flask-limiter.

    Returns a JSON body with a retry_after hint so API consumers can back off
    gracefully instead of hammering the server.

    Security note: Rate limiting protects against:
      - OWASP API5:2023 — Broken Function Level Authorization (resource exhaustion)
      - CWE-770 — Allocation of Resources Without Limits or Throttling
    """
    logger.warning(
        f"Rate limit exceeded for {request.remote_addr} on {request.path}. "
        f"UA: {request.headers.get('User-Agent', 'unknown')}"
    )
    return jsonify({
        "error": "Rate Limit Exceeded",
        "message": "Too many requests. Maximum 15 requests per hour per endpoint.",
        "retry_after": 3600
    }), 429


# =============================================================================
# BLOCK 4 — LOGGING & GLOBAL CONSTANTS
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Browser-like User-Agent to avoid being blocked by IRRES.be's server
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# =============================================================================
# BLOCK 5 — UTILITY: secure_get() & TYPE_MAPPING
# =============================================================================

# Maps English/raw property type strings from IRRES.be to Dutch display names
TYPE_MAPPING = {
    'Dwelling': 'Huis',
    'Flat':     'Appartement',
    'Land':     'Grond',
    'dwelling': 'Huis',
    'flat':     'Appartement',
    'land':     'Grond',
}


def secure_get(url, headers=None, timeout=15):
    """
    Secure wrapper around requests.get() that enforces HTTPS and TLS validation.

    Args:
        url     : Target URL. Will be upgraded to HTTPS automatically if needed.
        headers : Optional dict of HTTP headers (defaults to global HEADERS).
        timeout : Request timeout in seconds (default: 15).

    Returns:
        requests.Response

    Raises:
        requests.RequestException : On any network or HTTP error.
        ValueError                : If the URL cannot be made secure.

    Security:
        - Upgrades http:// → https:// transparently.
        - Always passes verify=True to enforce TLS certificate validation.
        - Raises on non-2xx HTTP status codes via raise_for_status().
    """
    # Upgrade HTTP to HTTPS
    if not url.startswith('https://'):
        if url.startswith('http://'):
            url = url.replace('http://', 'https://', 1)
        else:
            # Treat as a relative path on irres.be
            if not url.startswith('irres.be'):
                url = f'https://irres.be/{url.lstrip("/")}'

    try:
        response = requests.get(
            url,
            headers=headers or HEADERS,
            timeout=timeout,
            verify=True   # Enforce TLS certificate validation — never disable
        )
        response.raise_for_status()
        return response
    except requests.RequestException as e:
        logger.error(f"secure_get failed for {url}: {e}")
        raise


# =============================================================================
# BLOCK 6 — CLASS: IRRESLocationScraper
# =============================================================================
#
# Scrapes all property locations and location groups from the IRRES.be filter
# sidebar on https://irres.be/te-koop.
#
# Why .search-data instead of .search-values:
#   The page has two <ul> elements in the city filter container:
#     • <ul class="search-data hidden">   — statically rendered in the HTML.
#                                           Contains ALL locations, with duplicates.
#     • <ul class="search-values hidden"> — populated by JavaScript at runtime.
#                                           Empty when fetched with requests.get().
#
#   Since we fetch the page with a plain HTTP request (no JS execution),
#   we MUST target .search-data. After parsing we deduplicate by label so the
#   result matches exactly what the JS-rendered dropdown shows.
# =============================================================================

class IRRESLocationScraper:
    """
    Scraper for extracting property locations and location groups from IRRES.be.
    """

    BASE_URL = "https://irres.be/te-koop"

    def __init__(self, timeout: int = 15):
        """
        Initialize the scraper.

        Args:
            timeout: HTTP request timeout in seconds (default: 15).
        """
        self.timeout = timeout
        self.all_locations  = []
        self.location_groups = {}

    # -------------------------------------------------------------------------
    # BLOCK 6a — IRRESLocationScraper.normalize_text()
    # -------------------------------------------------------------------------

    @staticmethod
    def normalize_text(text: str) -> str:
        """
        Strip accents and diacritics from a UTF-8 string.

        Uses Unicode NFD decomposition to separate base letters from combining
        marks, then removes the combining marks (category 'Mn').

        Example: 'Sint-Denijs-Westrem' → 'Sint-Denijs-Westrem' (unchanged)
                 'Pétegem'             → 'Petegem'
        """
        nfd = unicodedata.normalize('NFD', text)
        return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')

    # -------------------------------------------------------------------------
    # BLOCK 6b — IRRESLocationScraper.fetch_page()
    # -------------------------------------------------------------------------

    def fetch_page(self) -> str:
        """
        Fetch the IRRES.be /te-koop page HTML via a secure HTTPS GET request.

        Returns:
            Full HTML of the page as a string.

        Raises:
            requests.RequestException: If the request fails for any reason.
        """
        try:
            logger.info(f"Fetching page: {self.BASE_URL}")
            response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
            logger.info(f"Page fetched successfully ({len(response.text):,} chars)")
            return response.text
        except requests.RequestException as e:
            logger.error(f"Failed to fetch locations page: {e}")
            raise

    # -------------------------------------------------------------------------
    # BLOCK 6c — IRRESLocationScraper.parse_locations()   ← FIXED
    # -------------------------------------------------------------------------

    def parse_locations(self, html_content: str):
        """
        Parse locations and location groups from the raw HTML of /te-koop.

        FIX EXPLANATION
        ---------------
        The previous implementation used soup.find_all('li', attrs={...}) over
        the entire document. This had two problems:

          1. It targeted <ul class="search-values"> which is JS-populated and
             therefore EMPTY when the page is fetched with requests.get().

          2. Even when items were found (e.g. via other li elements on the page),
             duplicates from the .search-data list caused inflated counts and
             inconsistent output.

        The fix:
          • Scope the search to the <div class="filter-container" data-category="city">
            container, which is always present in the static HTML.
          • Within that container, target <ul class="search-data"> — the static
            list that is always pre-rendered and contains all location options
            (with intentional duplicates that must be removed).
          • Deduplicate by data-label using an ordered dict so the result
            matches the JS dropdown exactly (first occurrence wins).

        Filtering:
          • Labels containing '€'           → price elements, skipped.
          • Labels in NON_LOCATION_TYPES     → property type filters, skipped.
          • Labels matching TYPE_MAPPING keys → English type strings, skipped.

        Returns:
            all_locations  : list of {"label": str, "value": str} dicts.
            location_groups: dict mapping label → list of sub-location strings.

        Output structure example:
            location_groups = {
                "Zwijnaarde": ["Zwijnaarde", "Gent Zwijnaarde"],
                "Nazareth - De Pinte": ["Nazareth-De Pinte", "Nazareth", "Eke",
                                        "De Pinte", "Zevergem"],
            }
        """
        soup = BeautifulSoup(html_content, 'html.parser')

        all_locations  = []
        location_groups = {}

        # ------------------------------------------------------------------
        # Step 1: Locate the city filter container.
        # Scoping to this element prevents accidentally picking up li elements
        # from other filter groups (property type, price range, etc.).
        # ------------------------------------------------------------------
        filter_container = soup.find(
            'div',
            attrs={
                'class':         lambda c: c and 'filter-container' in c,
                'data-category': 'city'
            }
        )

        if not filter_container:
            logger.warning(
                "City filter-container not found on page. "
                "Falling back to full-document search — results may include "
                "non-location items. The IRRES.be page structure may have changed."
            )
            filter_container = soup

        # ------------------------------------------------------------------
        # Step 2: Find the .search-data <ul>.
        # This list is statically rendered and always present in the raw HTML.
        # (Unlike .search-values which is populated by JavaScript at runtime.)
        # ------------------------------------------------------------------
        search_data_ul = filter_container.find('ul', class_='search-data')

        if not search_data_ul:
            logger.error(
                "search-data <ul> not found inside the city filter container. "
                "The IRRES.be page structure has likely changed."
            )
            return all_locations, location_groups

        li_elements = search_data_ul.find_all(
            'li',
            attrs={'data-label': True, 'data-value': True}
        )
        logger.info(
            f"Found {len(li_elements)} raw <li> elements in .search-data "
            f"(before deduplication)"
        )

        # ------------------------------------------------------------------
        # Step 3: Define filter blocklists.
        # These values appear in filter <li> elements on the page but are
        # property types, not location names.
        # ------------------------------------------------------------------
        NON_LOCATION_TYPES = {
            'Huis', 'Appartement', 'Grond',
            'Kantoor', 'Garage', 'Parking',
            'Opbrengsteigendom', 'Handelspand',
            'Industrieel', 'Commercieel', 'Project',
        }
        MAPPED_TYPES = set(TYPE_MAPPING.values())   # Dutch names from TYPE_MAPPING

        # ------------------------------------------------------------------
        # Step 4: Iterate, filter, and deduplicate.
        # Using a plain dict (ordered in Python 3.7+) as a seen-set so the
        # first occurrence of each label is kept — matching JS dropdown order.
        # ------------------------------------------------------------------
        seen_labels: dict = {}

        for li in li_elements:
            label = li.get('data-label', '').strip()
            value = li.get('data-value', '').strip()

            # Skip incomplete entries
            if not label or not value:
                continue

            # Skip price filter elements
            if '€' in label:
                continue

            # Skip property type elements (Dutch names)
            if label in NON_LOCATION_TYPES or label in MAPPED_TYPES:
                continue

            # Skip property type elements (English / raw names from TYPE_MAPPING)
            if label in TYPE_MAPPING or label.lower() in TYPE_MAPPING:
                continue

            # Skip duplicates — first occurrence of each label wins
            if label in seen_labels:
                continue
            seen_labels[label] = True

            # Parse comma-separated sub-locations, stripping whitespace
            sub_locations = [loc.strip() for loc in value.split(',') if loc.strip()]

            all_locations.append({"label": label, "value": label})
            location_groups[label] = sub_locations

        logger.info(
            f"Parsed {len(all_locations)} unique location groups after "
            f"deduplication and filtering"
        )
        return all_locations, location_groups

    # -------------------------------------------------------------------------
    # BLOCK 6d — IRRESLocationScraper.scrape()
    # -------------------------------------------------------------------------

    def scrape(self) -> dict:
        """
        Main entry point: fetch the page and parse all locations.

        Returns a dict with keys:
            all_locations  : list of {"label": str, "value": str}
            location_groups: dict mapping label → [sub-location, ...]
            count          : number of unique location groups found
            status         : "success" | "warning" | "error"
            error          : error message string (only present on "error")
        """
        try:
            html_content = self.fetch_page()
            all_locations, location_groups = self.parse_locations(html_content)

            self.all_locations   = all_locations
            self.location_groups = location_groups

            status = "success"
            if not all_locations:
                status = "warning"
                logger.warning(
                    "IRRESLocationScraper returned 0 locations. "
                    "The IRRES.be page structure may have changed."
                )

            return {
                "all_locations":   all_locations,
                "location_groups": location_groups,
                "count":           len(all_locations),
                "status":          status,
            }

        except Exception as e:
            logger.error(f"IRRESLocationScraper.scrape() failed: {e}")
            return {
                "all_locations":   [],
                "location_groups": {},
                "count":           0,
                "status":          "error",
                "error":           str(e),
            }


# =============================================================================
# BLOCK 7 — CLASS: IRRESOfficeImagesScraper
# =============================================================================
#
# Scrapes office photos from https://irres.be/contact.
#
# Page structure (from live HTML inspection):
#   Each office has a top-level section with a unique id, e.g. id="gent".
#   Inside: two sibling divs — one for text/address, one for the photo.
#   The <img> tag uses a 1×1 SVG placeholder in src (lazy loading).
#   The real image URL is always in the srcset attribute.
#
# Office id mapping used:
#   'gent'             → IrresGentImage
#   'sint-martens-latem' (or 'latem', 'sml') → IrresLatemImage
#   'destelbergen'     → IrresDestelbergenImage
# =============================================================================

class IRRESOfficeImagesScraper:
    """
    Scraper for extracting office images from IRRES.be/contact.
    """

    BASE_URL = "https://irres.be/contact"

    # Each tuple: (result_key, [candidate HTML id values tried in order])
    OFFICE_ID_MAP = [
        ('IrresGentImage',         ['gent']),
        ('IrresLatemImage',        ['sint-martens-latem', 'latem', 'sml']),
        ('IrresDestelbergenImage', ['destelbergen']),
    ]

    def __init__(self, timeout: int = 10):
        """
        Initialize the scraper.

        Args:
            timeout: HTTP request timeout in seconds (default: 10).
        """
        self.timeout = timeout

    # -------------------------------------------------------------------------
    # BLOCK 7a — IRRESOfficeImagesScraper.fetch_page()
    # -------------------------------------------------------------------------

    def fetch_page(self) -> str:
        """
        Fetch the IRRES.be /contact page HTML via a secure HTTPS GET request.

        Returns:
            Full HTML of the page as a string.

        Raises:
            requests.RequestException: If the request fails.
        """
        try:
            logger.info(f"Fetching page: {self.BASE_URL}")
            response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
            logger.info(f"Contact page fetched ({len(response.text):,} chars)")
            return response.text
        except requests.RequestException as e:
            logger.error(f"Failed to fetch contact page: {e}")
            raise

    # -------------------------------------------------------------------------
    # BLOCK 7b — IRRESOfficeImagesScraper.extract_image_url_from_section()
    # -------------------------------------------------------------------------

    @staticmethod
    def extract_image_url_from_section(section) -> str:
        """
        Find the first valid (non-placeholder) image URL within a page section.

        Priority order for source attributes:
          1. <img srcset>         — primary source on IRRES.be (real URL)
          2. <img data-srcset>    — alternative lazy-load attribute
          3. <source srcset>      — inside <picture> elements
          4. <img src>            — only if not a data: URI placeholder
          5. <img data-src> / <img data-lazy-src> / <img data-original>

        Any value starting with 'data:' is treated as a placeholder and skipped.
        Root-relative URLs (starting with '/') are made absolute using irres.be.

        Args:
            section: BeautifulSoup element representing one office section.

        Returns:
            Absolute image URL string, or '' if no valid image was found.
        """

        def parse_srcset(srcset_value: str) -> str:
            """Extract the first URL from a srcset string (strips width descriptors)."""
            if not srcset_value:
                return ''
            first_entry = srcset_value.split(',')[0].strip()
            return first_entry.split(' ')[0].strip()

        def make_absolute(url: str) -> str:
            """Convert a root-relative URL to an absolute irres.be URL."""
            if not url or url.startswith('data:'):
                return ''
            if url.startswith('http'):
                return url
            return ('https://irres.be' + url) if url.startswith('/') else f'https://irres.be/{url}'

        # 1 — <img srcset>
        for img in section.find_all('img'):
            srcset = img.get('srcset', '')
            if srcset and not srcset.startswith('data:'):
                url = make_absolute(parse_srcset(srcset))
                if url:
                    return url

        # 2 — <img data-srcset>
        for img in section.find_all('img'):
            srcset = img.get('data-srcset', '')
            if srcset and not srcset.startswith('data:'):
                url = make_absolute(parse_srcset(srcset))
                if url:
                    return url

        # 3 — <source srcset> inside <picture>
        for source in section.find_all('source'):
            srcset = source.get('srcset', '')
            if srcset and not srcset.startswith('data:'):
                url = make_absolute(parse_srcset(srcset))
                if url:
                    return url

        # 4 & 5 — Fallback: direct src / lazy-load attrs on <img>
        for img in section.find_all('img'):
            for attr in ('src', 'data-src', 'data-lazy-src', 'data-original'):
                val = img.get(attr, '')
                if val and not val.startswith('data:'):
                    url = make_absolute(val)
                    if url:
                        return url

        return ''

    # -------------------------------------------------------------------------
    # BLOCK 7c — IRRESOfficeImagesScraper.parse_office_images()
    # -------------------------------------------------------------------------

    def parse_office_images(self, html_content: str) -> dict:
        """
        Parse office images from the contact page HTML.

        Strategy:
          For each office in OFFICE_ID_MAP, try each candidate id in order.
          Use soup.find(id=...) to locate the section directly.
          Call extract_image_url_from_section() to pull the image URL.
          If a section id is not found, log a warning and continue.

        Args:
            html_content: Raw HTML string of the contact page.

        Returns:
            dict mapping result_key (e.g. 'IrresGentImage') to absolute URL.
        """
        soup   = BeautifulSoup(html_content, 'html.parser')
        images = {}

        for result_key, id_candidates in self.OFFICE_ID_MAP:
            section = None

            for section_id in id_candidates:
                section = soup.find(id=section_id)
                if section:
                    logger.info(f"Found section id='{section_id}' for {result_key}")
                    break

            if not section:
                logger.warning(
                    f"No section found for {result_key} "
                    f"(tried ids: {id_candidates}). "
                    f"The IRRES.be contact page structure may have changed."
                )
                continue

            image_url = self.extract_image_url_from_section(section)

            if image_url:
                images[result_key] = image_url
                logger.info(f"{result_key} → {image_url}")
            else:
                logger.warning(
                    f"Section id='{section_id}' found for {result_key} "
                    f"but no valid image URL could be extracted."
                )

        logger.info(
            f"Office image scraping complete: "
            f"{len(images)}/{len(self.OFFICE_ID_MAP)} images found"
        )
        return images

    # -------------------------------------------------------------------------
    # BLOCK 7d — IRRESOfficeImagesScraper.scrape()
    # -------------------------------------------------------------------------

    def scrape(self) -> dict:
        """
        Main entry point: fetch the contact page and extract all office images.

        Returns a dict with keys:
            status : "success" | "error"
            images : dict of image_key → absolute URL (empty dict on error)
            count  : number of images found (0 on error)
            error  : error message string (only present on "error")
        """
        try:
            html_content = self.fetch_page()
            images       = self.parse_office_images(html_content)

            return {
                "status": "success",
                "images": images,
                "count":  len(images),
            }

        except Exception as e:
            logger.error(f"IRRESOfficeImagesScraper.scrape() failed: {e}")
            return {
                "status": "error",
                "images": {},
                "count":  0,
                "error":  str(e),
            }


# =============================================================================
# BLOCK 8 — LISTING HELPER: normalize_text()
# =============================================================================

def normalize_text(s):
    """
    Normalize a string for clean display output.

    Operations (in order):
      1. Coerce to str (handles None, numbers, etc.).
      2. Unescape HTML entities (e.g. &nbsp;, &#178; for ²).
      3. Decode literal unicode escape sequences (e.g. \\u00b2 → ²).
      4. Collapse all internal whitespace to single spaces.
      5. Strip leading / trailing whitespace.

    Args:
        s: Any value (will be coerced to str).

    Returns:
        Normalized string, or '' if input is None or coercion fails.
    """
    if s is None:
        return ""

    try:
        s = str(s)
    except Exception:
        return ""

    # Decode HTML entities
    s = html.unescape(s)

    # Decode literal unicode escape sequences
    if "\\u" in s or "\\x" in s:
        try:
            s = bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            pass

    # Collapse whitespace
    s = " ".join(s.split())
    return s.strip()


# =============================================================================
# BLOCK 9 — LISTING HELPER: normalize_url()
# =============================================================================

def normalize_url(src, add_tracking=False):
    """
    Make a URL absolute for the IRRES.be domain.

    Handles the following input formats:
      - Protocol-relative  : //irres.be/...        → https://irres.be/...
      - Root-relative      : /uploads_c/...         → https://irres.be/uploads_c/...
      - Already absolute   : https://...            → unchanged
      - www-prefixed       : www.irres.be/...       → https://www.irres.be/...
      - Bare relative path : uploads_c/img.jpg      → https://irres.be/uploads_c/img.jpg

    Args:
        src          : Raw URL string (may be None or contain quotes).
        add_tracking : If True and the URL contains '/pand/', appends
                       ?origin=habichat (or &origin=habichat) for analytics.

    Returns:
        Absolute URL string, or '' if src is empty.
    """
    if not src:
        return ""

    src = src.strip().strip('\"\'')

    if src.startswith("//"):
        url = "https:" + src
    elif src.startswith("/"):
        url = "https://irres.be" + src
    elif re.match(r'https?://', src, re.I):
        url = src
    elif src.startswith("www."):
        url = "https://" + src
    elif not re.search(r':', src):
        # Bare relative path — no scheme or port
        url = "https://irres.be/" + src.lstrip('/')
    else:
        url = src

    # Append tracking parameter to listing detail URLs
    if add_tracking and '/pand/' in url:
        separator = '&' if '?' in url else '?'
        url = f"{url}{separator}origin=habichat"

    return url


# =============================================================================
# BLOCK 10 — LISTING HELPER: extract_listing_id_from_url()
# =============================================================================

def extract_listing_id_from_url(url):
    """
    Extract the numeric listing ID from a URL with the pattern /pand/<id>/.

    Examples:
        'https://irres.be/pand/1234/mijn-huis' → '1234'
        'https://irres.be/contact'              → ''

    Args:
        url: Absolute URL string.

    Returns:
        Numeric ID as a string, or '' if the pattern is not found.
    """
    if not url:
        return ""
    m = re.search(r'/pand/(\d+)', url)
    return m.group(1) if m else ""


# =============================================================================
# BLOCK 11 — LISTING HELPER: format_price_string()
# =============================================================================

def format_price_string(raw):
    """
    Format a raw price string into a clean, consistent display value.

    Output rules:
      - Numeric price  : '€ 1.085.000'  (dot as thousands separator, no decimals)
      - On request     : 'Prijs op aanvraag'
      - Under contract : 'Compromis in opmaak'
      - Empty input    : ''
      - Unrecognized   : normalized original string (fallback)

    Args:
        raw: Raw price string from the scraped listing card.

    Returns:
        Formatted price string.
    """
    if not raw:
        return ""

    s = normalize_text(raw)

    # Named status strings
    if re.search(r'Prijs op aanvraag', s, re.I):
        return "Prijs op aanvraag"
    if re.search(r'Compromis', s, re.I):
        return "Compromis in opmaak"

    # Extract numeric part
    cleaned = s.replace('€', '').replace('\u20ac', '')
    cleaned = re.sub(r'(?i)prijs op aanvraag|compromis.*', '', cleaned).strip()
    digits  = re.sub(r'[^0-9]', '', cleaned)

    if not digits:
        return s   # Fallback: return normalized original

    try:
        num       = int(digits)
        formatted = format(num, ',').replace(',', '.')   # Dot as thousands separator
        return f"€ {formatted}"
    except Exception:
        return s


# =============================================================================
# BLOCK 12 — LISTING HELPER: format_details_as_string()
# =============================================================================

def format_details_as_string(details_dict):
    """
    Serialize a property details dictionary to a semicolon-separated string.

    Only fields with non-empty values are included.

    Example output:
        'Terrein_oppervlakte: 8073 m²; Bewoonbare_oppervlakte: 264 m²; EPC: A'

    Args:
        details_dict: Dict mapping detail field names to values.

    Returns:
        Semicolon-separated string, or '' if dict is empty / all values empty.
    """
    if not details_dict:
        return ""

    parts = []
    for key, value in details_dict.items():
        if value and str(value).strip():
            parts.append(f"{key}: {value}")

    return "; ".join(parts)


# =============================================================================
# BLOCK 13 — LISTING HELPER: parse_main_listing_card()
# =============================================================================

def parse_main_listing_card(link):
    """
    Parse a single listing card <a> element from the /te-koop overview page.

    Extracts:
      - listing_url   : Absolute URL to the detail page (with tracking param).
      - location      : City / area name from the estate-city <h2>.
      - price_raw     : Raw price string (may contain '€', 'Prijs op aanvraag', etc.)
      - description   : Short description text (first non-price, non-type part).
      - listing_type  : Property type mapped to Dutch (Huis / Appartement / Grond).
      - photo_candidate: Best image URL found on the card element.
      - anchor_name   : Value of the name/data-name attribute (used as listing_id).

    Heuristic parsing:
      The listing card text is split on '|' separators. Parts are classified as
      price, type, or description based on content patterns.

    Args:
        link: BeautifulSoup <a> element representing one listing card.

    Returns:
        dict with keys listed above.
    """
    # --- Listing URL ---
    href = normalize_text(link.get('href') or "")
    if href and not href.startswith('http'):
        href = normalize_url(href, add_tracking=True)
    elif href.startswith('http') and '/pand/' in href:
        separator = '&' if '?' in href else '?'
        href = f"{href}{separator}origin=habichat"

    # --- Anchor name (used as listing_id on the site) ---
    anchor_name = normalize_text(link.get('name') or link.get('data-name') or "")

    # --- Location from <h2 class="estate-city"> ---
    location  = ""
    city_h2   = link.find('h2', class_=re.compile(r'estate-city'))
    if city_h2:
        location = city_h2.get('data-value', '').strip()
        if not location:
            location = normalize_text(city_h2.get_text())

    # --- Parse text content into parts ---
    text  = link.get_text(separator="|", strip=True)
    text  = normalize_text(text)
    parts = [normalize_text(x) for x in text.split("|") if normalize_text(x)]

    price        = ""
    description  = ""
    listing_type = ""

    for p in parts:
        # Price candidate
        if '€' in p or re.search(r'Prijs op aanvraag', p, re.I) or re.search(r'Compromis', p, re.I):
            price = p
            continue

        # Property type candidate
        if p in TYPE_MAPPING or p in TYPE_MAPPING.values() or p.lower() in TYPE_MAPPING:
            listing_type = TYPE_MAPPING.get(p, TYPE_MAPPING.get(p.lower(), p))
            continue

        # Description: first remaining part that isn't the location
        if not description and p != location and p != listing_type:
            description = p

    # Fallback description from last part
    if not description and len(parts) >= 2:
        possible = parts[-1]
        if possible not in TYPE_MAPPING and '€' not in possible and possible != location:
            description = possible

    # --- Photo from card ---
    photo_url = find_photo_on_element(link)

    return {
        "listing_url":    href,
        "location":       location,
        "price_raw":      price,
        "description":    description,
        "listing_type":   listing_type,
        "photo_candidate": photo_url,
        "anchor_name":    anchor_name,
    }


# =============================================================================
# BLOCK 14 — LISTING HELPER: find_photo_on_element()
# =============================================================================

def find_photo_on_element(el):
    """
    Find the best image URL within a BeautifulSoup element.

    Checks (in order):
      1. <img> tag attributes: src, data-src, data-lazy-src, data-original, srcset, data-srcset
      2. <source> tag attributes: srcset, data-srcset, src
      3. Inline CSS style: background-image: url(...)
      4. Element-level data attributes: data-src, data-image, data-bg, data-photo, etc.

    Preference:
      URLs containing '/uploads', 'uploads_c', '/siteassets', or '/panden',
      or having common image extensions (.jpg, .jpeg, .png, .webp, .gif)
      are preferred over other candidates.

    Args:
        el: BeautifulSoup element to search within.

    Returns:
        Absolute image URL string, or '' if nothing valid was found.
    """
    candidates = []

    # 1 — <img> tag attributes
    for img in el.find_all('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'srcset', 'data-srcset'):
            v = img.get(attr)
            if v:
                if 'srcset' in attr:
                    candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                else:
                    candidates.append(v)

    # 2 — <source> tags
    for source in el.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            v = source.get(attr)
            if v:
                if 'srcset' in attr:
                    candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                else:
                    candidates.append(v)

    # 3 — Inline style background-image
    for node in ([el] + el.find_all(True)):
        style = node.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m:
                candidates.append(m.group(1))

    # 4 — Element-level data attributes
    for attr in ('data-src', 'data-image', 'data-bg', 'data-photo', 'data-thumb', 'data-original'):
        v = el.get(attr)
        if v:
            candidates.append(v)

    # Normalize: make absolute, remove data: URIs and SVG placeholders
    normed = []
    for c in candidates:
        if not c:
            continue
        c = normalize_url(c)
        if c and not c.startswith('data:') and 'svg' not in c.lower():
            normed.append(c)

    # Prefer upload-path URLs and known image extensions
    for n in normed:
        if re.search(r'/uploads|uploads_c|/siteassets|/panden', n, re.I) or \
           re.search(r'\.(jpg|jpeg|png|webp|gif)(?:\?|$)', n, re.I):
            return n

    # Fallback: return first candidate
    return normed[0] if normed else ""


# =============================================================================
# BLOCK 15 — LISTING HELPER: find_landscape_image_from_detail()
# =============================================================================

def find_landscape_image_from_detail(soup):
    """
    Search for the best property photo on a detail page.

    Scopes the search to <main data-barba="container"> when available, which
    avoids picking up header / navigation images. Falls back to the full
    document if the main element is not found.

    Preference: URLs containing upload paths and image file extensions
    (indicating actual property photos rather than icons or placeholders).

    Args:
        soup: BeautifulSoup object of the detail page.

    Returns:
        Absolute image URL string, or '' if nothing suitable was found.
    """
    main         = soup.find('main', attrs={'data-barba': True, 'data-barba-namespace': True})
    search_scope = main if main else soup
    candidates   = []

    # Collect from <img> tags
    for img in search_scope.find_all('img'):
        for attr in ('srcset', 'data-srcset'):
            srcset = img.get(attr)
            if srcset:
                candidates.extend([p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()])
        for attr in ('src', 'data-src', 'data-original', 'data-lazy-src'):
            v = img.get(attr)
            if v:
                candidates.append(v)

    # Collect from <picture> / <source> tags
    for source in search_scope.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            v = source.get(attr)
            if v:
                if 'srcset' in attr:
                    candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                else:
                    candidates.append(v)

    # Collect from inline style background-image
    for el in search_scope.find_all(style=True):
        style = el.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m:
                candidates.append(m.group(1))

    # Normalize and filter
    normed = []
    for c in candidates:
        if not c:
            continue
        c = normalize_url(c)
        if c and not c.startswith('data:') and 'svg' not in c.lower():
            normed.append(c)

    # Prefer upload-path URLs with image extensions
    for n in normed:
        if re.search(r'/uploads|uploads_c|siteassets|/panden', n, re.I) and \
           re.search(r'\.(jpg|jpeg|png|webp)', n, re.I):
            return n

    return normed[0] if normed else ""


# =============================================================================
# BLOCK 16 — LISTING HELPER: extract_property_details_from_detail_soup()
# =============================================================================

def extract_property_details_from_detail_soup(soup):
    """
    Extract property specification details from the 'Kenmerken' section of a
    detail page.

    Looks for <li data-value="..."> elements and maps them to known field names
    using case-insensitive partial keyword matching on the data-value attribute.
    The field value is taken from the first <p> inside the <li>, or from the
    <li>'s own text if no <p> is present.

    Recognised fields and their match keywords:
      Terrein_oppervlakte   → 'terrein'
      Bewoonbare_oppervlakte→ 'bewoonbare'
      Terras_oppervlakte    → 'terras'
      Orientatie            → 'ori'
      Slaapkamers           → 'slaap'
      Badkamers             → 'bad'
      Bouwjaar              → 'bouw' (and NOT 'reno')
      Renovatiejaar         → 'reno'
      EPC                   → 'epc'
      Beschikbaarheid       → 'beschik'

    Args:
        soup: BeautifulSoup object of the detail page.

    Returns:
        dict with all ten keys above; values are '' for any field not found.
    """
    details = {
        "Terrein_oppervlakte":    "",
        "Bewoonbare_oppervlakte": "",
        "Terras_oppervlakte":     "",
        "Orientatie":             "",
        "Slaapkamers":            "",
        "Badkamers":              "",
        "Bouwjaar":               "",
        "Renovatiejaar":          "",
        "EPC":                    "",
        "Beschikbaarheid":        "",
    }

    try:
        lis = soup.find_all('li', attrs={'data-value': True})

        for li in lis:
            key   = li.get('data-value', '').strip()
            p_tag = li.find('p')
            value = normalize_text(p_tag.get_text() if p_tag else li.get_text())

            if not value:
                continue

            key_lower = key.lower()

            if 'terrein' in key_lower:
                details["Terrein_oppervlakte"]    = value
            elif 'terras' in key_lower:
                details["Terras_oppervlakte"]     = value
            elif 'bewoonbare' in key_lower:
                details["Bewoonbare_oppervlakte"] = value
            elif 'ori' in key_lower:
                details["Orientatie"]             = value
            elif 'slaap' in key_lower:
                details["Slaapkamers"]            = value
            elif 'bad' in key_lower:
                details["Badkamers"]              = value
            elif 'bouw' in key_lower and 'reno' not in key_lower:
                details["Bouwjaar"]               = value
            elif 'reno' in key_lower:
                details["Renovatiejaar"]          = value
            elif 'epc' in key_lower:
                details["EPC"]                    = value
            elif 'beschik' in key_lower:
                details["Beschikbaarheid"]        = value

    except Exception:
        pass   # Return partially-filled dict on any error

    return details


# =============================================================================
# BLOCK 17 — LISTING HELPER: extract_contact_and_email_from_detail()
# =============================================================================

def extract_contact_and_email_from_detail(soup):
    """
    Extract the agent's email address and first name from a listing detail page.

    Strategy:
      1. Search for <a href="mailto:..."> links — most reliable source.
      2. Fallback: scan full page text for an email pattern (regex).

    First-name extraction:
      The local part of the email address (before @) is split on dot/underscore/
      hyphen and the first token is capitalised as the first name.
      Example: 'jan.peeters@irres.be' → first_name='Jan'

    Args:
        soup: BeautifulSoup object of the detail page.

    Returns:
        Tuple (first_name: str, email: str). Both are '' if not found.
    """
    email      = ""
    first_name = ""

    # 1 — mailto: links
    all_mailto = soup.find_all('a', href=re.compile(r'^mailto:', re.I))
    for a in all_mailto:
        href = a.get('href', '')
        if not href:
            continue
        m = re.search(r'mailto:([^?]+)', href)
        if m:
            candidate = normalize_text(m.group(1))
            if candidate:
                email = candidate
                break

    # 2 — Regex fallback on page text
    if not email:
        text = soup.get_text(" ", strip=True)
        m2   = re.search(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
        if m2:
            email = normalize_text(m2.group(1))

    # Derive first name from email local part
    if email:
        local       = email.split('@')[0]
        local_token = re.split(r'[._\-]', local)[0]
        if local_token:
            first_name = local_token.capitalize()

    return first_name, email


# =============================================================================
# BLOCK 18 — LISTING HELPER: fetch_detail_page()
# =============================================================================

def fetch_detail_page(url, timeout=12):
    """
    Fetch and parse a listing detail page.

    Uses secure_get() to enforce HTTPS and TLS validation.

    Args:
        url    : Absolute URL of the detail page.
        timeout: Request timeout in seconds (default: 12).

    Returns:
        BeautifulSoup object of the parsed page, or None if the request fails.
    """
    try:
        response = secure_get(url, headers=HEADERS, timeout=timeout)
        return BeautifulSoup(response.content, 'html.parser')
    except Exception:
        return None


# =============================================================================
# BLOCK 19 — API ENDPOINT: /api/listings
# =============================================================================

@limiter.limit("15 per hour")
@app.route('/api/listings', methods=['GET'])
def get_listings():
    """
    Scrape all active property listings from IRRES.be/te-koop.

    This is the most expensive endpoint:
      - Initial overview page fetch      : ~15 seconds
      - Per-listing detail page fetches  : 15–20 requests × ~10 seconds each
      - Total wall-clock time            : 3–8 minutes per request

    Rate limit: 15 requests per hour per IP (protects both this server and IRRES.be).

    Response JSON structure:
        {
            "success" : true,
            "count"   : <int>,
            "listings": [
                {
                    "listing_id"  : "<str>",
                    "listing_url" : "<str>",
                    "photo_url"   : "<str>",
                    "price"       : "<str>",
                    "location"    : "<str>",
                    "description" : "<str>",
                    "listing_type": "<str>",
                    "Title"       : "<str>",
                    "Button1_Label": "Bekijk het op onze website",
                    "Button2_Label": "<str>",
                    "Button2_email": "<str>",
                    "Button3_Label": "<str>",
                    "Button3_Value": "<str>",
                    "details"     : "<str>"
                },
                ...
            ]
        }
    """
    try:
        list_page_url = "https://irres.be/te-koop"
        resp          = secure_get(list_page_url, headers=HEADERS, timeout=15)
        soup          = BeautifulSoup(resp.content, 'html.parser')

        # ----------------------------------------------------------------
        # Find all listing anchor elements (links to /pand/<id>/ URLs)
        # ----------------------------------------------------------------
        anchors = soup.find_all('a', href=re.compile(r'/pand/\d+/', re.I))

        seen          = set()
        listing_links = []

        for a in anchors:
            href = a.get('href') or ""
            if not href:
                continue

            full = normalize_url(href, add_tracking=True) if not href.startswith('http') else href
            if full.startswith('http') and '/pand/' in full:
                separator = '&' if '?' in full else '?'
                full      = f"{full}{separator}origin=habichat"

            if full in seen:
                continue

            # Only include anchors with text content (listing cards have text)
            text = a.get_text(separator="|", strip=True)
            if not text or not text.strip():
                continue

            seen.add(full)
            listing_links.append(a)

        # ----------------------------------------------------------------
        # Process each listing card
        # ----------------------------------------------------------------
        listings = []

        for link in listing_links:
            parsed = parse_main_listing_card(link)

            listing_url = parsed['listing_url'] or ""
            if not listing_url:
                continue

            listing_id_num  = extract_listing_id_from_url(listing_url)
            parsed_location = parsed.get('location') or ""

            # Map listing type to Dutch display name
            lt        = parsed['listing_type'] or ""
            lt_mapped = TYPE_MAPPING.get(lt, TYPE_MAPPING.get(lt.lower(), lt)) if lt else ""

            photo_url = parsed['photo_candidate'] or ""

            # ----------------------------------------------------------------
            # Fetch detail page for contact info and property specifications
            # ----------------------------------------------------------------
            time.sleep(0.09)   # Polite crawl delay — don't hammer IRRES.be
            detail_soup = fetch_detail_page(listing_url)

            button2_label = ""
            button2_email = ""
            details       = {
                "Terrein_oppervlakte":    "",
                "Bewoonbare_oppervlakte": "",
                "Terras_oppervlakte":     "",
                "Orientatie":             "",
                "Slaapkamers":            "",
                "Badkamers":              "",
                "Bouwjaar":               "",
                "Renovatiejaar":          "",
                "EPC":                    "",
                "Beschikbaarheid":        "",
            }

            if detail_soup:
                # Contact info
                first_name, email = extract_contact_and_email_from_detail(detail_soup)
                if email:
                    button2_email = f"mailto:{email}"
                    name_label    = first_name if first_name else email.split('@')[0]
                    name_label    = " ".join(
                        [p.capitalize() for p in re.split(r'[._\-]', name_label) if p]
                    )
                    button2_label = f"Contacteer {name_label} - Irres"

                # Property specifications
                details_found = extract_property_details_from_detail_soup(detail_soup)
                for k in details.keys():
                    if details_found.get(k):
                        details[k] = details_found[k]

                # Fallback photo from detail page if card had none
                if not photo_url:
                    fallback = find_landscape_image_from_detail(detail_soup)
                    if fallback:
                        photo_url = fallback

            # ----------------------------------------------------------------
            # Build display fields
            # ----------------------------------------------------------------
            photo_url       = normalize_url(photo_url) if photo_url else ""
            price_formatted = format_price_string(parsed['price_raw']) if parsed['price_raw'] else ""

            # Title: "{Location}⎥{Price}"
            Title = ""
            if parsed_location or price_formatted:
                if parsed_location and price_formatted:
                    Title = f"{parsed_location}⎥{price_formatted}"
                else:
                    Title = parsed_location or price_formatted

            # Recover location from Title if it was missing on the card
            if (not parsed_location) and Title and '⎥' in Title:
                possible_loc = Title.split('⎥', 1)[0].strip()
                if possible_loc and '€' not in possible_loc:
                    parsed_location = normalize_text(possible_loc)

            # listing_id: prefer anchor name from site, else number+location
            anchor_name = parsed.get('anchor_name') or ""
            if anchor_name:
                listing_id = anchor_name
            else:
                location_for_id = parsed_location.split()[0] if parsed_location else ""
                location_for_id = re.sub(r'[^A-Za-z0-9\-]', '', location_for_id)
                listing_id      = f"{listing_id_num}-{location_for_id}" if listing_id_num else listing_url

            # Button3: only shown when price is on request
            button3_label = "Vraag prijs aan" if price_formatted == "Prijs op aanvraag" else ""
            button3_value = (
                f"{button2_email}?subject=Prijs aanvraag {listing_id}"
                if price_formatted == "Prijs op aanvraag"
                else ""
            )

            details_string = format_details_as_string(details)

            listing_obj = {
                "listing_id":    listing_id,
                "listing_url":   listing_url,
                "photo_url":     photo_url,
                "price":         price_formatted,
                "location":      parsed_location,
                "description":   parsed.get('description') or "",
                "listing_type":  lt_mapped,
                "Title":         Title,
                "Button1_Label": "Bekijk het op onze website",
                "Button2_Label": button2_label,
                "Button2_email": button2_email,
                "Button3_Label": button3_label,
                "Button3_Value": button3_value,
                "details":       details_string,
            }

            # Only include listings that have at least some meaningful content
            if listing_obj.get("location") or listing_obj.get("price") or listing_obj.get("description"):
                listings.append(listing_obj)

        # ----------------------------------------------------------------
        # Deduplicate by listing_id
        # ----------------------------------------------------------------
        uniq     = []
        seen_ids = set()
        for li in listings:
            lid = li.get("listing_id")
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            uniq.append(li)

        payload = {
            "success":  True,
            "count":    len(uniq),
            "listings": uniq,
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        )

    except Exception as e:
        payload = {
            "success":  False,
            "error":    str(e),
            "listings": [],
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        ), 200


# =============================================================================
# BLOCK 20 — API ENDPOINT: /api/locations
# =============================================================================

@limiter.limit("15 per hour")
@app.route('/api/locations', methods=['GET'])
def get_locations():
    """
    Return all available property filter locations and their sub-location groups.

    Rate limit: 15 requests per hour per IP.

    Query parameters:
        format : 'json' (default) | 'csv'
                 CSV returns a flat list of location group names, one per line.

    JSON response structure:
        {
            "status"   : "success",
            "timestamp": "<ISO 8601>",
            "data": {
                "all_locations"  : [{"label": "<str>", "value": "<str>"}, ...],
                "location_groups": {
                    "<label>": ["<sub-location>", ...],
                    ...
                },
                "count": <int>
            }
        }

    CSV response:
        location
        Gent
        Gent + deelgemeenten
        Zwijnaarde
        ...
    """
    try:
        logger.info("GET /api/locations — fetching locations from IRRES.be")

        scraper = IRRESLocationScraper()
        result  = scraper.scrape()

        output_format = request.args.get('format', 'json').lower()

        if output_format == 'csv':
            csv_content = "location\n"
            csv_content += "\n".join([loc['label'] for loc in result['all_locations']])
            return Response(
                response=csv_content,
                status=200,
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment;filename=irres_locations.csv"}
            )

        # Default: JSON
        response_body = {
            "status":    "success",
            "timestamp": datetime.now().isoformat(),
            "data": {
                "all_locations":   result['all_locations'],
                "location_groups": result['location_groups'],
                "count":           len(result['all_locations']),
            }
        }

        logger.info(f"Successfully retrieved {len(result['all_locations'])} location groups")
        return jsonify(response_body), 200

    except Exception as e:
        logger.error(f"GET /api/locations failed: {e}")
        return jsonify({
            "status":    "error",
            "timestamp": datetime.now().isoformat(),
            "message":   str(e),
        }), 500


# =============================================================================
# BLOCK 21 — API ENDPOINT: /api/office-images
# =============================================================================

@limiter.limit("15 per hour")
@app.route('/api/office-images', methods=['GET'])
def get_office_images():
    """
    Return the photo URLs for all IRRES offices, scraped from IRRES.be/contact.

    Rate limit: 15 requests per hour per IP.

    JSON response structure (success):
        {
            "status"   : "success",
            "timestamp": "<ISO 8601>",
            "data": {
                "IrresGentImage"         : "<absolute URL>",
                "IrresLatemImage"        : "<absolute URL>",
                "IrresDestelbergenImage" : "<absolute URL>"
            }
        }

    JSON response structure (error):
        {
            "status"   : "error",
            "timestamp": "<ISO 8601>",
            "data"     : {}
        }
    """
    try:
        logger.info("GET /api/office-images — fetching office images from IRRES.be")

        scraper = IRRESOfficeImagesScraper()
        result  = scraper.scrape()

        response_body = {
            "status":    result['status'],
            "timestamp": datetime.now().isoformat(),
            "data":      result['images'],
        }

        if result['status'] == 'success':
            logger.info(f"Successfully retrieved {result['count']} office images")
            return jsonify(response_body), 200
        else:
            return jsonify(response_body), 500

    except Exception as e:
        logger.error(f"GET /api/office-images failed: {e}")
        return jsonify({
            "status":    "error",
            "timestamp": datetime.now().isoformat(),
            "message":   str(e),
        }), 500


# =============================================================================
# BLOCK 22 — API ENDPOINTS: /health & / (root)
# =============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint.

    Returns HTTP 200 with a simple JSON body so load balancers and uptime
    monitors (e.g. UptimeRobot, Render.com health checks) can verify the
    service is alive without triggering authentication or rate limiting.

    Not rate-limited (exempted in BLOCK 2).
    """
    return jsonify({
        "status":    "healthy",
        "timestamp": datetime.now().isoformat(),
        "service":   "IRRES Unified Scraper",
    })


@app.route('/', methods=['GET'])
def root():
    """
    Root endpoint — API discovery / documentation.

    Returns a JSON overview of all available endpoints, their purpose,
    and the current API version. Useful for quick reference without
    consulting external documentation.

    Not rate-limited (exempted in BLOCK 2).
    """
    return jsonify({
        "api":     "IRRES.be Unified Scraper",
        "version": "7.0",
        "endpoints": {
            "/api/listings": (
                "Scrape all active property listings with contact info and "
                "property details. Expensive — allow 3–8 minutes."
            ),
            "/api/locations": (
                "Get all filter locations and sub-location groups. "
                "Supports ?format=csv for a flat CSV export."
            ),
            "/api/office-images": (
                "Get absolute image URLs for all IRRES offices."
            ),
            "/health": "Health check — returns 200 if the service is running.",
        },
        "authentication": "X-API-KEY header required on all endpoints except /health.",
        "rate_limits":    "15 requests per hour per IP on scraping endpoints.",
    })


# =============================================================================
# BLOCK 23 — RUN SERVER
# =============================================================================

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
