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
#   BLOCK 12 — Listing Helper: parse_main_listing_card()
#   BLOCK 13 — Listing Helper: find_photo_on_element()
#   BLOCK 14 — Listing Helper: find_landscape_image_from_detail()
#   BLOCK 15 — Listing Helper: extract_address_from_detail_soup()
#   BLOCK 16 — Listing Helper: extract_page_content_from_detail_soup()
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
import uuid
import logging
import contextvars
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from flask import Flask, jsonify, Response, request, g, has_request_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import requests
from bs4 import BeautifulSoup

# -----------------------------------------------------------------------------
# IRRES logging (inlined; was logging_config.py). Env: LOG_LEVEL, LOG_JSON=1
# -----------------------------------------------------------------------------

_sync_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "irres_sync_run_id", default=None
)


class IrresContextFilter(logging.Filter):
    """Adds request_id / client / path; uses sync run id when outside Flask."""

    def filter(self, record: logging.LogRecord) -> bool:
        req_id = "-"
        client = "-"
        path = "-"
        try:
            if has_request_context():
                req_id = getattr(g, "request_id", None) or "-"
                client = request.remote_addr or "-"
                path = request.path or "-"
        except Exception:
            pass

        if req_id == "-":
            sid = _sync_run_id.get()
            if sid:
                req_id = sid

        if getattr(record, "run_id", None):
            req_id = str(record.run_id)

        record.request_id = req_id
        record.client = getattr(record, "client", None) or client
        record.path = getattr(record, "path", None) or path
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "client": getattr(record, "client", "-"),
            "path": getattr(record, "path", "-"),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TerminalFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt=(
                "%(asctime)s | %(levelname)-8s | %(name)s | "
                "request_id=%(request_id)s client=%(client)s path=%(path)s | %(message)s"
            ),
            datefmt="%Y-%m-%dT%H:%M:%S",
        )


_CONTEXT_FILTER = IrresContextFilter()


def _attach_filter_to_handlers() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if _CONTEXT_FILTER not in h.filters:
            h.addFilter(_CONTEXT_FILTER)


def configure_logging(service: str = "irres", level: str | None = None) -> None:
    _ = service
    raw = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    resolved = getattr(logging, raw, logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved)

    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(resolved)
        if os.getenv("LOG_JSON") == "1":
            h.setFormatter(JsonFormatter())
        else:
            h.setFormatter(TerminalFormatter())
        h.addFilter(_CONTEXT_FILTER)
        root.addHandler(h)
    else:
        _attach_filter_to_handlers()


# Initialize Flask App
app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False  # Ensure UTF-8 characters are preserved in JSON output

configure_logging(service="api")
logger = logging.getLogger("irres.api")


@app.before_request
def _irres_request_log_start():
    if request.endpoint == "static":
        return
    g.request_id = str(uuid.uuid4())
    if request.endpoint in ("get_listings", "get_locations", "get_office_images"):
        g._irres_req_t0 = time.perf_counter()
        logger.info(
            "event=http_request_start method=%s path=%s endpoint=%s",
            request.method,
            request.path,
            request.endpoint,
        )


@app.after_request
def _irres_request_log_done(response):
    t0 = getattr(g, "_irres_req_t0", None)
    if t0 is not None:
        ms = int((time.perf_counter() - t0) * 1000)
        summary = getattr(g, "_log_summary", None)
        if summary:
            logger.info(
                "event=http_request_done path=%s status=%s duration_ms=%s %s",
                request.path,
                response.status_code,
                ms,
                summary,
            )
        else:
            logger.info(
                "event=http_request_done path=%s status=%s duration_ms=%s",
                request.path,
                response.status_code,
                ms,
            )
    return response


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
limiter.exempt(lambda: request.endpoint == 'health_check')
limiter.exempt(lambda: request.endpoint == 'root')


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
      1. Static files and GET /health are exempt (uptime monitors; no key in URL).
      2. Any request with ?api_key=... in the query string is rejected (401).
      3. Missing X-API-KEY header → 401 Unauthorized (except exempt routes above).
      4. Wrong X-API-KEY value   → 401 Unauthorized.
      5. Correct header          → request proceeds normally.
    """
    # Allow static files and health checks through without authentication
    if request.endpoint in ('static', 'health_check'):
        return

    # Explicitly block query-parameter authentication attempts
    if 'api_key' in request.args:
        logger.warning(
            "event=security_reject_query_api_key client=%s path=%s detail=use_X_API_KEY_header",
            request.remote_addr,
            request.path,
        )
        return jsonify({
            "error": "Unauthorized",
            "message": "API key must be provided via X-API-KEY header, not query parameters"
        }), 401

    # Extract key from header
    provided_api_key = request.headers.get('X-API-KEY')

    if not provided_api_key:
        logger.warning(
            "event=security_missing_api_key client=%s path=%s",
            request.remote_addr,
            request.path,
        )
        return jsonify({
            "error": "Unauthorized",
            "message": "X-API-KEY header is required"
        }), 401

    if provided_api_key != API_KEY:
        logger.warning(
            "event=security_invalid_api_key client=%s path=%s",
            request.remote_addr,
            request.path,
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
        "event=rate_limit_exceeded client=%s path=%s ua=%s",
        request.remote_addr,
        request.path,
        request.headers.get("User-Agent", "unknown"),
    )
    return jsonify({
        "error": "Rate Limit Exceeded",
        "message": "Too many requests. Maximum 15 requests per hour per endpoint.",
        "retry_after": 3600
    }), 429


# =============================================================================
# BLOCK 4 — GLOBAL CONSTANTS (logging configured after Flask app init)
# =============================================================================

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
        logger.debug("event=http_get_failed url=%s error=%s", url, e)
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
    # BLOCK 6a — IRRESLocationScraper.fetch_page()
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
            logger.debug("event=http_get_start url=%s", self.BASE_URL)
            response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
            logger.debug(
                "event=http_get_ok url=%s bytes=%s",
                self.BASE_URL,
                len(response.text),
            )
            return response.text
        except requests.RequestException as e:
            logger.debug("event=locations_fetch_failed url=%s error=%s", self.BASE_URL, e)
            raise

    # -------------------------------------------------------------------------
    # BLOCK 6b — IRRESLocationScraper.parse_locations()   ← FIXED
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
                "event=locations_parse_fallback client=parser detail=city_filter_container_missing"
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
                "event=locations_search_data_missing detail=irres_page_structure_changed"
            )
            return all_locations, location_groups

        li_elements = search_data_ul.find_all(
            'li',
            attrs={'data-label': True, 'data-value': True}
        )
        logger.debug(
            "event=locations_raw_li_count count=%s phase=before_dedup",
            len(li_elements),
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

            all_locations.append({"label": label, "value": value})
            location_groups[label] = sub_locations

        logger.debug(
            "event=locations_parsed_unique count=%s phase=after_dedup",
            len(all_locations),
        )
        return all_locations, location_groups

    # -------------------------------------------------------------------------
    # BLOCK 6c — IRRESLocationScraper.scrape()
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
            t0 = time.perf_counter()
            html_content = self.fetch_page()
            all_locations, location_groups = self.parse_locations(html_content)

            self.all_locations   = all_locations
            self.location_groups = location_groups

            status = "success"
            if not all_locations:
                status = "warning"
                logger.warning(
                    "event=locations_scrape_empty detail=irres_page_structure_may_have_changed"
                )

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(
                "event=locations_scrape_done count=%s status=%s duration_ms=%s",
                len(all_locations),
                status,
                elapsed_ms,
            )

            return {
                "all_locations":   all_locations,
                "location_groups": location_groups,
                "count":           len(all_locations),
                "status":          status,
            }

        except Exception as e:
            logger.exception("event=locations_scrape_failed")
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
# Each office block is identified by a title <p> with specific utility classes.
# Image URLs must contain "kantoor-" and live under uploads_c/siteassets.
# =============================================================================

class IRRESOfficeImagesScraper:
    """Scraper for office images from IRRES.be/contact."""

    BASE_URL = "https://irres.be/contact"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def fetch_page(self) -> str:
        try:
            logger.debug("event=http_get_start url=%s", self.BASE_URL)
            response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
            logger.debug(
                "event=http_get_ok url=%s bytes=%s",
                self.BASE_URL,
                len(response.text),
            )
            return response.text
        except requests.RequestException as e:
            logger.debug("event=office_fetch_failed url=%s error=%s", self.BASE_URL, e)
            raise

    @staticmethod
    def _p_is_office_title(p) -> bool:
        c = p.get("class")
        if not c:
            return False
        s = " ".join(c) if isinstance(c, list) else str(c)
        needed = (
            "font-serif",
            "text-2xl",
            "mb-8",
            "border-b-2",
            "border-black",
            "pb-8",
        )
        return all(x in s for x in needed)

    @staticmethod
    def _is_kantoor_asset_url(u: str) -> bool:
        if not u or u.startswith("data:"):
            return False
        ul = u.lower()
        return "kantoor-" in ul and "uploads_c" in ul and "siteassets" in ul

    @classmethod
    def _collect_kantoor_images(cls, container) -> list:
        seen = set()
        urls = []
        if not container:
            return urls

        for img in container.find_all("img"):
            for attr in ("srcset", "data-srcset"):
                u = best_url_from_srcset(img.get(attr) or "")
                if u and cls._is_kantoor_asset_url(u) and u not in seen:
                    seen.add(u)
                    urls.append(u)
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                v = img.get(attr) or ""
                if not v or v.startswith("data:"):
                    continue
                u = normalize_url(v)
                if u and cls._is_kantoor_asset_url(u) and u not in seen:
                    seen.add(u)
                    urls.append(u)

        for source in container.find_all("source"):
            u = best_url_from_srcset(source.get("srcset") or "")
            if u and cls._is_kantoor_asset_url(u) and u not in seen:
                seen.add(u)
                urls.append(u)

        return urls

    @staticmethod
    def _office_block_container(title_p):
        el = title_p.parent
        for _ in range(14):
            if not el or not getattr(el, "name", None):
                break
            if el.name and el.name.lower() == "section":
                return el
            if el.find("img"):
                return el
            el = el.parent
        return title_p.parent

    def parse_office_images(self, html_content: str) -> list:
        soup = BeautifulSoup(html_content, "html.parser")
        out = []
        for title_p in soup.find_all("p"):
            if not self._p_is_office_title(title_p):
                continue
            office_name = normalize_text(title_p.get_text())
            if not office_name:
                continue
            container = self._office_block_container(title_p)
            for image_url in self._collect_kantoor_images(container):
                out.append({"office_name": office_name, "image_url": image_url})

        logger.debug("event=office_parse_rows count=%s", len(out))
        return out

    def scrape(self) -> dict:
        try:
            t0 = time.perf_counter()
            html_content = self.fetch_page()
            images = self.parse_office_images(html_content)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(
                "event=office_scrape_done count=%s status=success duration_ms=%s",
                len(images),
                elapsed_ms,
            )
            return {
                "status": "success",
                "images": images,
                "count":  len(images),
            }
        except Exception as e:
            logger.exception("event=office_scrape_failed")
            return {
                "status": "error",
                "images": [],
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


def canonical_listing_url(href):
    """
    Absolute listing URL without query string or fragment.
    Example: https://irres.be/pand/8942402/elegant-landhuis-in-de-eikeldreef
    """
    if not href:
        return ""
    h = normalize_text(href)
    u = normalize_url(h) if h else ""
    if not u:
        return ""
    u = u.split("#")[0].split("?")[0].rstrip("/")
    return u


def parse_srcset_entries(srcset_str):
    """
    Parse a srcset string into (raw_url, width_px) tuples.
    Width is 0 when no descriptor; callers may treat last entry as largest when all 0.
    """
    if not srcset_str or srcset_str.startswith("data:"):
        return []
    out = []
    for part in srcset_str.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        raw_u = bits[0].strip()
        w = 0
        if len(bits) >= 2:
            m = re.search(r"(\d+)w", bits[1], re.I)
            if m:
                w = int(m.group(1))
        out.append((raw_u, w))
    return out


def best_url_from_srcset(srcset_str):
    """
    Pick the image URL with the largest width descriptor from a srcset.
    If all widths are 0, return the last URL (common responsive-image ordering).
    """
    entries = parse_srcset_entries(srcset_str)
    if not entries:
        return ""
    abs_entries = []
    for raw_u, w in entries:
        u = normalize_url(raw_u)
        if u and not u.startswith("data:") and "svg" not in u.lower():
            abs_entries.append((u, w))
    if not abs_entries:
        return ""
    max_w = max(e[1] for e in abs_entries)
    if max_w > 0:
        return max(abs_entries, key=lambda x: x[1])[0]
    return abs_entries[-1][0]


def listing_button1_value(listing_url):
    """Listing URL with utm_source=habichat for outbound links."""
    if not listing_url:
        return ""
    sep = "&" if "?" in listing_url else "?"
    return f"{listing_url}{sep}utm_source=habichat"


def display_name_from_email(email):
    """Human label from contact email local-part (e.g. daphne@ → Daphne)."""
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    return " ".join(p.capitalize() for p in re.split(r"[._\-]", local) if p)


def is_prijs_op_aanvraag_price(price_str):
    return price_str in ("Prijs op aanvraag", "Vraag prijs aan")


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
    if re.search(r'Vraag\s+prijs\s+aan', s, re.I):
        return "Vraag prijs aan"
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
# BLOCK 12 — LISTING HELPER: parse_main_listing_card()
# =============================================================================

def parse_main_listing_card(link):
    """
    Parse a single listing card <a> element from the /te-koop overview page.

    Extracts:
      - listing_url   : Canonical absolute URL (no tracking query).
      - location      : City / area from estate-city <h2>.
      - price_raw     : Raw price from card text (superseded by detail page when available).
      - description   : Prefer <p class="text-18 ... mb-10 ... leading-7">.
      - listing_type  : Dutch label (Huis / Appartement / Grond) via TYPE_MAPPING.
      - photo_candidate: Best image URL on the card (widest srcset candidate).
      - anchor_name   : name / data-name attribute (required listing_id on site).
    """
    # --- Listing URL (canonical, no query) ---
    href = normalize_text(link.get("href") or "")
    listing_url = canonical_listing_url(href)

    # --- Anchor name (site listing_id) ---
    anchor_name = normalize_text(link.get("name") or link.get("data-name") or "")

    # --- Location from <h2 class="estate-city"> ---
    location = ""
    city_h2 = link.find("h2", class_=re.compile(r"estate-city"))
    if city_h2:
        location = city_h2.get("data-value", "").strip()
        if not location:
            location = normalize_text(city_h2.get_text())

    # --- Property type: estate-type element (English) before mapping ---
    listing_type_raw = ""
    et_el = link.find(class_=re.compile(r"estate-type"))
    if et_el:
        listing_type_raw = normalize_text(et_el.get_text())

    # --- Description: dedicated teaser paragraph when present ---
    description = ""

    def _classes_include(c, *needles):
        if not c:
            return False
        s = " ".join(c) if isinstance(c, list) else str(c)
        return all(n in s for n in needles)

    desc_p = link.find(
        "p",
        class_=lambda c: _classes_include(c, "text-18", "mb-10", "leading-7"),
    )
    if desc_p:
        description = normalize_text(desc_p.get_text())

    # --- Parse text content into parts (price + fallback description / type) ---
    text = link.get_text(separator="|", strip=True)
    text = normalize_text(text)
    parts = [normalize_text(x) for x in text.split("|") if normalize_text(x)]

    price = ""
    listing_type = ""

    for p in parts:
        if "€" in p or re.search(r"Prijs op aanvraag", p, re.I) or re.search(r"Compromis", p, re.I):
            price = p
            continue
        if p in TYPE_MAPPING or p in TYPE_MAPPING.values() or p.lower() in TYPE_MAPPING:
            if not listing_type_raw:
                listing_type_raw = p
            listing_type = TYPE_MAPPING.get(p, TYPE_MAPPING.get(p.lower(), p))
            continue
        if not description and p != location and p != listing_type:
            description = p

    if listing_type_raw:
        listing_type = TYPE_MAPPING.get(
            listing_type_raw,
            TYPE_MAPPING.get(listing_type_raw.lower(), listing_type_raw),
        )

    if not description and len(parts) >= 2:
        possible = parts[-1]
        if possible not in TYPE_MAPPING and "€" not in possible and possible != location:
            description = possible

    photo_url = find_photo_on_element(link)

    return {
        "listing_url":       listing_url,
        "location":          location,
        "price_raw":         price,
        "description":       description,
        "listing_type":      listing_type,
        "photo_candidate":   photo_url,
        "anchor_name":       anchor_name,
    }


# =============================================================================
# BLOCK 13 — LISTING HELPER: find_photo_on_element()
# =============================================================================

def find_photo_on_element(el):
    """
    Find the best image URL within a BeautifulSoup element.

    Prefers widest srcset candidate, then upload paths / image extensions.
    """
    candidates = []

    for img in el.find_all("img"):
        for attr in ("srcset", "data-srcset"):
            u = best_url_from_srcset(img.get(attr) or "")
            if u:
                candidates.append(u)
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            v = img.get(attr)
            if v:
                candidates.append(v)

    for source in el.find_all("source"):
        u = best_url_from_srcset(source.get("srcset") or source.get("data-srcset") or "")
        if u:
            candidates.append(u)

    for node in [el] + el.find_all(True):
        style = node.get("style") or ""
        if "url(" in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m:
                candidates.append(m.group(1))

    for attr in ("data-src", "data-image", "data-bg", "data-photo", "data-thumb", "data-original"):
        v = el.get(attr)
        if v:
            candidates.append(v)

    normed = []
    for c in candidates:
        if not c:
            continue
        c = normalize_url(c)
        if c and not c.startswith("data:") and "svg" not in c.lower():
            normed.append(c)

    for n in normed:
        if re.search(r"/uploads|uploads_c|/siteassets|/panden", n, re.I) or re.search(
            r"\.(jpg|jpeg|png|webp|gif)(?:\?|$)", n, re.I
        ):
            return n

    return normed[0] if normed else ""


# =============================================================================
# BLOCK 14 — LISTING HELPER: find_landscape_image_from_detail()
# =============================================================================

def find_landscape_image_from_detail(soup):
    """
    Search for the best property photo on a detail page (widest srcset wins).
    """
    main = (
        soup.find("main", attrs={"data-barba": True, "data-barba-namespace": True})
        or soup.find("main", attrs={"data-barba": True})
        or soup.find("main")
    )
    search_scope = main if main else soup
    scored = []

    for img in search_scope.find_all("img"):
        for attr in ("srcset", "data-srcset"):
            entries = parse_srcset_entries(img.get(attr) or "")
            for raw_u, w in entries:
                u = normalize_url(raw_u)
                if u and not u.startswith("data:") and "svg" not in u.lower():
                    scored.append((w, u))
        for attr in ("src", "data-src", "data-original", "data-lazy-src"):
            v = img.get(attr)
            if v and not v.startswith("data:"):
                u = normalize_url(v)
                if u:
                    scored.append((0, u))

    for source in search_scope.find_all("source"):
        entries = parse_srcset_entries(source.get("srcset") or source.get("data-srcset") or "")
        for raw_u, w in entries:
            u = normalize_url(raw_u)
            if u and not u.startswith("data:"):
                scored.append((w, u))

    for el in search_scope.find_all(style=True):
        style = el.get("style") or ""
        if "url(" in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m:
                u = normalize_url(m.group(1))
                if u:
                    scored.append((0, u))

    prop = [
        (w, u)
        for w, u in scored
        if re.search(r"/uploads|uploads_c|siteassets|/panden", u, re.I)
        and re.search(r"\.(jpg|jpeg|png|webp)", u, re.I)
    ]
    pool = prop if prop else [(w, u) for w, u in scored if u]
    if not pool:
        return ""
    pool.sort(key=lambda x: x[0], reverse=True)
    return pool[0][1]


# =============================================================================
# BLOCK 15 — LISTING HELPER: extract_address_from_detail_soup()
# =============================================================================

def _is_listing_address_header_div(classes):
    """
    IRRES shows the property address only in the hero column:
    <div class="lg:w-1/2 w-full text-20 leading-6">...</div>
    Wider matching (e.g. any lg:w-1/2) would pick up office addresses elsewhere.
    """
    if not classes:
        return False
    s = " ".join(classes) if isinstance(classes, list) else str(classes)
    return (
        "lg:w-1/2" in s
        and "text-20" in s
        and "leading-6" in s
    )


def _looks_like_street_line(s):
    """Heuristic: Belgian street lines usually contain a house number or a street keyword."""
    if not s or len(s) < 3:
        return False
    if re.search(r"\d", s):
        return True
    low = s.lower()
    keywords = (
        "laan", "straat", "steenweg", "weg", "lei", "dreef", "park", "plein",
        "baan", "dorp", "hof", "pad", "route", "square", "rue", "avenue",
        "gracht", "kaai", "wal", "residentie", "site", "zone",
    )
    return any(k in low for k in keywords)


def _belgian_postal_city_line(s):
    """Second line of a typical IRRES address: 4-digit postcode + locality."""
    return bool(s and re.match(r"^\d{4}\s+\S", s))


def _format_address_pair(line1, line2):
    if line1 and line2:
        return f"{line1}, {line2}"
    return ""


def _paragraph_texts_for_listing_address(container):
    """
    Collect <p> text from the listing address column only.
    Skips <p> inside <a> (e.g. 'Toon ligging') and inside iframes / embedded map UI.
    """
    from bs4 import Tag

    out = []

    # Prefer direct child <p> (canonical layout: two lines then map link)
    for child in container.children:
        if isinstance(child, Tag) and child.name == "p":
            t = normalize_text(child.get_text())
            if t:
                out.append(t)

    if len(out) >= 2:
        return out

    # Wrapped layout: gather <p> in tree order, exclude chrome / links
    out = []
    for p in container.find_all("p"):
        if p.find_parent("a"):
            continue
        if p.find_parent("iframe"):
            continue
        t = normalize_text(p.get_text())
        if not t:
            continue
        # Google Maps embed sometimes exposes stray short labels
        if p.get("class") and any("gm-" in str(c) for c in p.get("class", [])):
            continue
        out.append(t)

    return out


def extract_address_from_detail_soup(soup):
    """
    Extract the property address only from the IRRES listing hero block:

        <div class="lg:w-1/2 w-full text-20 leading-6">...</div>

    No other regions of the page are scanned (avoids office addresses in footer
    or elsewhere). Returns "" when that block has no valid street + postal pair.
    """
    try:
        for container in soup.find_all("div", class_=lambda c: _is_listing_address_header_div(c)):
            texts = _paragraph_texts_for_listing_address(container)
            for i in range(len(texts) - 1):
                a, b = texts[i], texts[i + 1]
                if _belgian_postal_city_line(b) and _looks_like_street_line(a):
                    return _format_address_pair(a, b)

        return ""

    except Exception as e:
        logger.warning("event=listing_address_extract_failed error=%s", e)
        return ""


# =============================================================================
# BLOCK 16 — LISTING HELPER: extract_page_content_from_detail_soup()
# =============================================================================

def extract_page_content_from_detail_soup(soup):
    """
    Serialize visible text under <main data-barba> in document order.
    Links become markdown [text](url). List items use "* " prefix.
    """
    from bs4 import NavigableString, Comment, Tag

    try:
        main = soup.find("main", attrs={"data-barba": True}) or soup.find("main")
        if not main:
            return ""

        SKIP = {"script", "style", "noscript", "template", "svg"}

        def hidden(tag):
            st = tag.get("style") or ""
            if "display:none" in st.replace(" ", "").lower():
                return True
            if tag.get("aria-hidden") == "true":
                return True
            return False

        def render_inline(tag):
            parts = []
            for child in tag.children:
                if isinstance(child, Comment):
                    continue
                if isinstance(child, NavigableString):
                    t = normalize_text(str(child))
                    if t:
                        parts.append(t)
                    continue
                if not isinstance(child, Tag):
                    continue
                if child.name.lower() in SKIP or hidden(child):
                    continue
                nm = child.name.lower()
                if nm == "br":
                    parts.append("\n")
                elif nm == "a" and child.get("href"):
                    href = normalize_url(child.get("href"))
                    txt = normalize_text(child.get_text())
                    parts.append(f"[{txt or href}]({href})")
                else:
                    parts.append(render_inline(child))
            return "".join(parts)

        def render_block(tag):
            if not isinstance(tag, Tag):
                return ""
            if hidden(tag):
                return ""
            nm = tag.name.lower()
            if nm in SKIP:
                return ""
            if nm == "nav":
                return ""

            if nm in ("h1", "h2", "h3", "h4", "h5", "h6"):
                t = normalize_text(tag.get_text(" ", strip=True))
                return (f"\n{t}\n" if t else "")

            if nm == "p":
                inner = normalize_text(render_inline(tag).strip())
                return (inner + "\n") if inner else ""

            if nm in ("ul", "ol"):
                lines = []
                for li in tag.find_all("li", recursive=False):
                    if hidden(li):
                        continue
                    lt = normalize_text(render_inline(li))
                    if lt:
                        lines.append(f"* {lt}")
                return ("\n".join(lines) + "\n") if lines else ""

            if nm == "a" and tag.get("href"):
                href = normalize_url(tag.get("href"))
                txt = normalize_text(tag.get_text())
                return f"[{txt or href}]({href})\n"

            if nm == "br":
                return "\n"

            out = []
            for child in tag.children:
                if isinstance(child, Comment):
                    continue
                if isinstance(child, NavigableString):
                    t = normalize_text(str(child))
                    if t:
                        out.append(t + "\n")
                elif isinstance(child, Tag):
                    out.append(render_block(child))
            return "".join(out)

        result = render_block(main)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    except Exception as e:
        logger.warning("event=listing_page_content_extract_failed error=%s", e)
        return ""


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
    email = ""
    first_name = ""

    search_roots = []
    main = soup.find("main", attrs={"data-barba": True}) or soup.find("main")
    if main:
        search_roots.append(main)
    search_roots.append(soup)

    for root in search_roots:
        all_mailto = root.find_all("a", href=re.compile(r"^mailto:", re.I))
        for a in all_mailto:
            href = a.get("href", "")
            if not href:
                continue
            m = re.search(r"mailto:([^?]+)", href)
            if m:
                candidate = normalize_text(m.group(1))
                if candidate:
                    email = candidate
                    break
        if email:
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
# BLOCK 17b — LISTING HELPER: extract_price_from_detail_soup()
# =============================================================================

def extract_price_from_detail_soup(soup):
    """Raw price line from the listing detail header."""
    main = soup.find("main", attrs={"data-barba": True}) or soup.find("main") or soup
    price_div = main.find("div", class_=re.compile(r"flex items-center text-lg"))
    if price_div:
        price_p = price_div.find("p")
        if price_p:
            return normalize_text(price_p.get_text())
    return ""


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
@app.route("/listings", methods=["GET"])
@app.route("/api/listings", methods=["GET"])
def get_listings():
    """
    Scrape all active property listings from IRRES.be/te-koop.

    This is the most expensive endpoint:
      - Initial overview page fetch      : ~15 seconds
      - Per-listing detail page fetches  : many sequential requests
      - Total wall-clock time            : several minutes per request

    Rate limit: 15 requests per hour per IP (protects both this server and IRRES.be).

    Each listing uses snake_case keys including title, button*_label/value,
    address, and page_content (full <main> text with markdown links).
    """
    try:
        t0 = time.perf_counter()
        list_page_url = "https://irres.be/te-koop"
        resp          = secure_get(list_page_url, headers=HEADERS, timeout=15)
        soup          = BeautifulSoup(resp.content, 'html.parser')

        anchors = soup.find_all('a', href=re.compile(r'/pand/\d+/', re.I))

        seen          = set()
        listing_links = []

        for a in anchors:
            href = a.get('href') or ""
            if not href:
                continue

            full = canonical_listing_url(href)
            if not full or full in seen:
                continue

            text = a.get_text(separator="|", strip=True)
            if not text or not text.strip():
                continue

            seen.add(full)
            listing_links.append(a)

        logger.info("event=listings_scrape_start links=%s", len(listing_links))

        listings = []

        for link in listing_links:
            parsed = parse_main_listing_card(link)

            anchor_name = parsed.get('anchor_name') or ""
            if not anchor_name:
                logger.debug(
                    "event=listing_skip_card reason=missing_listing_id anchor_name_empty"
                )
                continue

            listing_url = parsed.get('listing_url') or ""
            if not listing_url:
                continue

            parsed_location = parsed.get('location') or ""
            lt_mapped         = parsed.get('listing_type') or ""

            photo_url = parsed.get('photo_candidate') or ""

            time.sleep(0.09)
            detail_soup = fetch_detail_page(listing_url)

            price_formatted = ""
            button2_label   = ""
            button2_value   = ""
            address         = ""
            page_content    = ""
            email           = ""

            if detail_soup:
                detail_price_raw = extract_price_from_detail_soup(detail_soup)
                if detail_price_raw:
                    price_formatted = format_price_string(detail_price_raw)
                if not price_formatted and parsed.get('price_raw'):
                    price_formatted = format_price_string(parsed['price_raw'])

                _, email = extract_contact_and_email_from_detail(detail_soup)
                if email:
                    button2_value = f"mailto:{email}"
                    nm = display_name_from_email(email) or email.split("@")[0].capitalize()
                    button2_label = f"Email {nm} - Irres"

                address      = extract_address_from_detail_soup(detail_soup)
                page_content = extract_page_content_from_detail_soup(detail_soup)

                if not photo_url:
                    fallback = find_landscape_image_from_detail(detail_soup)
                    if fallback:
                        photo_url = fallback
            elif parsed.get('price_raw'):
                price_formatted = format_price_string(parsed['price_raw'])

            photo_url = normalize_url(photo_url) if photo_url else ""

            title = ""
            if parsed_location and price_formatted:
                title = f"{parsed_location}⎥{price_formatted}"
            elif parsed_location or price_formatted:
                title = parsed_location or price_formatted

            if (not parsed_location) and title and '⎥' in title:
                possible_loc = title.split('⎥', 1)[0].strip()
                if possible_loc and '€' not in possible_loc:
                    parsed_location = normalize_text(possible_loc)

            listing_id      = anchor_name
            button1_label   = "Bekijk het op onze website"
            button1_value   = listing_button1_value(listing_url)

            prijs_aanvraag = is_prijs_op_aanvraag_price(price_formatted)
            button3_label  = "Vraag prijs aan" if prijs_aanvraag else ""
            button3_value  = ""
            if prijs_aanvraag and email:
                subj = f"Prijs aanvraag {listing_id}"
                button3_value = f"mailto:{email}?subject={quote(subj)}"

            listing_obj = {
                "listing_id":     listing_id,
                "listing_url":    listing_url,
                "listing_type":   lt_mapped,
                "photo_url":      photo_url,
                "title":          title,
                "price":          price_formatted,
                "location":       parsed_location,
                "description":    parsed.get('description') or "",
                "button1_label":  button1_label,
                "button1_value":  button1_value,
                "button2_label":  button2_label,
                "button2_value":  button2_value,
                "button3_label":  button3_label,
                "button3_value":  button3_value,
                "address":        address,
                "page_content":   page_content,
            }

            if listing_obj.get("location") or listing_obj.get("price") or listing_obj.get("description"):
                listings.append(listing_obj)

        uniq     = []
        seen_ids = set()
        for li in listings:
            lid = li.get("listing_id")
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            uniq.append(li)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "event=listings_scrape_done count=%s duration_ms=%s",
            len(uniq),
            elapsed_ms,
        )
        g._log_summary = "listings_count=%s" % len(uniq)

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
        logger.exception("event=listings_scrape_failed http_status=500 success_json=false")
        payload = {
            "success":  False,
            "error":    str(e),
            "listings": [],
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        ), 500


# =============================================================================
# BLOCK 20 — API ENDPOINT: /api/locations
# =============================================================================

@limiter.limit("15 per hour")
@app.route("/locations", methods=["GET"])
@app.route("/api/locations", methods=["GET"])
def get_locations():
    """
    Return all available property filter locations and their sub-location groups.

    Rate limit: 15 requests per hour per IP.

    Query parameters:
        format : 'json' (default) | 'csv'
                 CSV returns a flat list of location group names, one per line.

    JSON response structure:
        {
            "status"   : "success" | "warning" | "error",
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
        scraper = IRRESLocationScraper()
        result  = scraper.scrape()
        inner_status = result.get("status") or "success"

        if inner_status == "error":
            return jsonify({
                "status":    "error",
                "timestamp": datetime.now().isoformat(),
                "message":   result.get("error") or "Location scrape failed.",
                "data": {
                    "all_locations":   result.get("all_locations") or [],
                    "location_groups": result.get("location_groups") or {},
                    "count":           0,
                },
            }), 502

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

        response_body = {
            "status":    inner_status,
            "timestamp": datetime.now().isoformat(),
            "data": {
                "all_locations":   result['all_locations'],
                "location_groups": result['location_groups'],
                "count":           len(result['all_locations']),
            },
        }

        return jsonify(response_body), 200

    except Exception as e:
        logger.exception("event=get_locations_failed")
        return jsonify({
            "status":    "error",
            "timestamp": datetime.now().isoformat(),
            "message":   str(e),
        }), 500


# =============================================================================
# BLOCK 21 — API ENDPOINT: /api/office-images
# =============================================================================

@limiter.limit("15 per hour")
@app.route("/office-images", methods=["GET"])
@app.route("/api/office-images", methods=["GET"])
def get_office_images():
    """
    Return the photo URLs for all IRRES offices, scraped from IRRES.be/contact.

    Rate limit: 15 requests per hour per IP.

    JSON response structure (success):
        {
            "status"   : "success",
            "timestamp": "<ISO 8601>",
            "data": [
                {"office_name": "<str>", "image_url": "<absolute URL>"},
                ...
            ]
        }

    JSON response structure (error):
        {
            "status"   : "error",
            "timestamp": "<ISO 8601>",
            "data"     : []
        }
    """
    try:
        scraper = IRRESOfficeImagesScraper()
        result  = scraper.scrape()

        response_body = {
            "status":    result['status'],
            "timestamp": datetime.now().isoformat(),
            "data":      result['images'],
        }

        if result['status'] == 'success':
            return jsonify(response_body), 200
        logger.warning(
            "event=get_office_images_scraper_error status=%s error=%s",
            result.get("status"),
            result.get("error", ""),
        )
        return jsonify(response_body), 500

    except Exception as e:
        logger.exception("event=get_office_images_failed")
        return jsonify({
            "status":    "error",
            "timestamp": datetime.now().isoformat(),
            "message":   str(e),
            "data":      [],
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
        "version": "9.0",
        "endpoints": {
            "/listings": "Same as /api/listings — full listing scrape (slow).",
            "/api/listings": (
                "Scrape all active property listings with contact info and "
                "full page content. Expensive — allow several minutes."
            ),
            "/locations": "Same as /api/locations — filter locations JSON or CSV.",
            "/api/locations": (
                "Get all filter locations and sub-location groups. "
                "Supports ?format=csv for a flat CSV export."
            ),
            "/office-images": "Same as /api/office-images — office photos from /contact.",
            "/api/office-images": (
                "List of {office_name, image_url} for IRRES offices (kantoor- assets)."
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
