# =============================================================================
# IRRES.be Scraper API
# Retrieves property listings, locations, and office images from https://irres.be/
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
app.config['JSON_AS_ASCII'] = False

# =============================================================================
# RATE LIMITING
# =============================================================================
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)
limiter.exempt(lambda: request.endpoint == 'health')
limiter.exempt(lambda: request.endpoint == 'index')

# =============================================================================
# SECURITY & AUTHENTICATION
# =============================================================================
API_KEY = os.getenv("API_KEY")
if API_KEY is None:
    raise ValueError("API_KEY environment variable is required but not set")


@app.before_request
def require_api_key():
    if request.endpoint == 'static':
        return
    if 'api_key' in request.args:
        logger.warning(
            f"SECURITY: Rejected ?api_key query param from {request.remote_addr} on {request.path}"
        )
        return jsonify({"error": "Unauthorized", "message": "API key must be provided via X-API-KEY header"}), 401
    provided_api_key = request.headers.get('X-API-KEY')
    if not provided_api_key:
        logger.warning(f"SECURITY: Missing X-API-KEY header from {request.remote_addr} on {request.path}")
        return jsonify({"error": "Unauthorized", "message": "X-API-KEY header is required"}), 401
    if provided_api_key != API_KEY:
        logger.warning(f"SECURITY: Invalid X-API-KEY from {request.remote_addr} on {request.path}")
        return jsonify({"error": "Unauthorized", "message": "Invalid X-API-KEY"}), 401
    return None


@app.errorhandler(429)
def ratelimit_handler(e):
    logger.warning(f"Rate limit exceeded for {request.remote_addr} on {request.path}")
    return jsonify({
        "error": "Rate Limit Exceeded",
        "message": "Too many requests",
        "retry_after": 3600
    }), 429


# =============================================================================
# LOGGING & CONSTANTS
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Type mapping
TYPE_MAPPING = {
    'Dwelling': 'Huis',
    'Flat': 'Appartement',
    'Land': 'Grond',
    'dwelling': 'Huis',
    'flat': 'Appartement',
    'land': 'Grond',
}

# UTF-8 symbol conversions
UTF8_CONVERSIONS = {
    '\u33a1': 'm2',  # U+33A1 (㎡) -> m2
    '\u20ac': '\u20ac',  # Keep euro symbol as is or convert to €
    '\u00b2': '2',  # Superscript 2
}


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================
def secure_get(url, headers=None, timeout=15):
    """Secure HTTPS GET request wrapper."""
    if not url.startswith('https://'):
        if url.startswith('http://'):
            url = url.replace('http://', 'https://', 1)
        else:
            url = f'https://irres.be/{url.lstrip("/")}'
    try:
        response = requests.get(
            url,
            headers=headers or HEADERS,
            timeout=timeout,
            verify=True
        )
        response.raise_for_status()
        return response
    except requests.RequestException as e:
        logger.error(f"secure_get failed for {url}: {e}")
        raise


def normalize_text(s):
    """Normalize text: unescape HTML, decode unicode, collapse whitespace."""
    if s is None:
        return ""
    try:
        s = str(s)
    except Exception:
        return ""
    s = html.unescape(s)
    if "\\u" in s or "\\x" in s:
        try:
            s = bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            pass
    # Apply UTF-8 conversions
    for old, new in UTF8_CONVERSIONS.items():
        s = s.replace(old, new)
    s = " ".join(s.split())
    return s.strip()


def normalize_url(src, add_tracking=False):
    """Convert relative URLs to absolute irres.be URLs."""
    if not src:
        return ""
    src = src.strip().strip('"\'')
    if src.startswith("//"):
        url = "https:" + src
    elif src.startswith("/"):
        url = "https://irres.be" + src
    elif re.match(r'https?://', src, re.I):
        url = src
    elif src.startswith("www."):
        url = "https://" + src
    else:
        url = f"https://irres.be/{src.lstrip('/')}"
    if add_tracking and '/pand/' in url:
        separator = '&' if '?' in url else '?'
        url = f"{url}{separator}utm_source=habichat"
    return url


def extract_listing_id_from_url(url):
    """Extract numeric ID from /pand/<id>/ URL."""
    if not url:
        return ""
    m = re.search(r'/pand/(\d+)', url)
    return m.group(1) if m else ""


def convert_utf8_symbols(text):
    """Convert special UTF-8 symbols to normal characters."""
    conversions = {
        '\u33a1': 'm2',
        '\u00b2': '2',
        '\u00b3': '3',
    }
    for old, new in conversions.items():
        text = text.replace(old, new)
    return text


# =============================================================================
# SCRAPER: Locations
# =============================================================================
class IRRESLocationScraper:
    BASE_URL = "https://irres.be/te-koop"

    def __init__(self, timeout=15):
        self.timeout = timeout

    def fetch_page(self):
        logger.info(f"Fetching page: {self.BASE_URL}")
        response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
        logger.info(f"Page fetched successfully ({len(response.text):,} chars)")
        return response.text

    def parse_locations(self, html_content):
        """Parse locations from .search-data ul element."""
        soup = BeautifulSoup(html_content, 'html.parser')
        all_locations = []
        location_groups = {}

        # Find the city filter container
        filter_container = soup.find(
            'div',
            attrs={
                'class': lambda c: c and 'filter-container' in c,
                'data-category': 'city'
            }
        )
        if not filter_container:
            filter_container = soup

        # Find the .search-data ul (static list with all locations)
        search_data_ul = filter_container.find('ul', class_='search-data')
        if not search_data_ul:
            logger.error("search-data <ul> not found")
            return all_locations, location_groups

        li_elements = search_data_ul.find_all('li', attrs={'data-label': True, 'data-value': True})
        logger.info(f"Found {len(li_elements)} raw <li> elements")

        # Filter out non-location items
        NON_LOCATION_TYPES = {
            'Huis', 'Appartement', 'Grond', 'Kantoor', 'Garage', 'Parking',
            'Opbrengsteigendom', 'Handelspand', 'Industrieel', 'Commercieel', 'Project',
        }
        MAPPED_TYPES = set(TYPE_MAPPING.values())

        seen_labels = {}
        for li in li_elements:
            label = li.get('data-label', '').strip()
            value = li.get('data-value', '').strip()

            if not label or not value:
                continue
            if '\u20ac' in label:
                continue
            if label in NON_LOCATION_TYPES or label in MAPPED_TYPES:
                continue
            if label in TYPE_MAPPING or label.lower() in TYPE_MAPPING:
                continue
            if label in seen_labels:
                continue
            seen_labels[label] = True

            sub_locations = [loc.strip() for loc in value.split(',') if loc.strip()]
            all_locations.append({"label": label, "value": label})
            location_groups[label] = sub_locations

        logger.info(f"Parsed {len(all_locations)} unique location groups")
        return all_locations, location_groups

    def scrape(self):
        try:
            html_content = self.fetch_page()
            all_locations, location_groups = self.parse_locations(html_content)
            status = "success" if all_locations else "warning"
            return {
                "all_locations": all_locations,
                "location_groups": location_groups,
                "count": len(all_locations),
                "status": status,
            }
        except Exception as e:
            logger.error(f"LocationScraper failed: {e}")
            return {
                "all_locations": [],
                "location_groups": {},
                "count": 0,
                "status": "error",
                "error": str(e),
            }


# =============================================================================
# SCRAPER: Office Images
# =============================================================================
class IRRESOfficeImagesScraper:
    BASE_URL = "https://irres.be/contact"

    def __init__(self, timeout=10):
        self.timeout = timeout

    def fetch_page(self):
        logger.info(f"Fetching page: {self.BASE_URL}")
        response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
        logger.info(f"Contact page fetched ({len(response.text):,} chars)")
        return response.text

    def parse_office_images(self, html_content):
        """Parse office images from contact page."""
        soup = BeautifulSoup(html_content, 'html.parser')
        offices = []

        # Find all sections with id (office sections)
        sections = soup.find_all(['section', 'div'], id=True)
        for section in sections:
            section_id = section.get('id', '')

            # Find office name from p.font-serif.text-2xl
            name_p = section.find('p', class_=lambda x: x and 'font-serif' in str(x) and 'text-2xl' in str(x))
            office_name = name_p.get_text(strip=True) if name_p else section_id

            # Find images with kantoor in srcset
            for img in section.find_all('img'):
                srcset = img.get('srcset', '')
                if srcset and 'kantoor' in srcset.lower():
                    # Extract first URL from srcset
                    first_entry = srcset.split(',')[0].strip()
                    image_url = first_entry.split(' ')[0].strip()
                    image_url = normalize_url(image_url)

                    # Check if image matches required pattern
                    if '/uploads_c/siteassets/' in image_url or 'kantoor-' in image_url:
                        offices.append({
                            "office_name": office_name,
                            "image_url": image_url
                        })
                        break

        logger.info(f"Found {len(offices)} office images")
        return offices

    def scrape(self):
        try:
            html_content = self.fetch_page()
            offices = self.parse_office_images(html_content)
            return {
                "status": "success",
                "offices": offices,
                "count": len(offices),
            }
        except Exception as e:
            logger.error(f"OfficeImagesScraper failed: {e}")
            return {
                "status": "error",
                "offices": [],
                "count": 0,
                "error": str(e),
            }


# =============================================================================
# SCRAPER: Listings
# =============================================================================
class IRRESListingsScraper:
    BASE_URL = "https://irres.be/te-koop"

    def __init__(self, timeout=15):
        self.timeout = timeout

    def fetch_page(self):
        logger.info(f"Fetching page: {self.BASE_URL}")
        response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
        logger.info(f"Page fetched successfully ({len(response.text):,} chars)")
        return response.text

    def parse_listing_card(self, container):
        """Parse a single listing card container."""
        # Find anchor with /pand/ in href
        anchor = container.find('a', href=re.compile(r'/pand/\d+/', re.I))
        if not anchor:
            return None

        # Extract listing_id from name attribute
        listing_id = anchor.get('name', '')
        if not listing_id:
            return None

        # Extract href
        href = anchor.get('href', '')
        listing_url = normalize_url(href)
        if '/pand/' not in listing_url:
            return None

        # Extract listing_id_number (numeric part)
        listing_id_number = extract_listing_id_from_url(listing_url)

        # Find estate-type (hidden p with class containing estate-type)
        estate_type_elem = container.find(class_=lambda x: x and 'estate-type' in str(x).lower())
        listing_type_raw = estate_type_elem.get_text(strip=True) if estate_type_elem else ""
        listing_type = TYPE_MAPPING.get(listing_type_raw, listing_type_raw)

        # Find estate-city (h2 with class containing estate-city)
        estate_city_elem = container.find(class_=lambda x: x and 'estate-city' in str(x).lower())
        location = ""
        if estate_city_elem:
            location = estate_city_elem.get('data-value', '').strip()
            if not location:
                location = estate_city_elem.get_text(strip=True)
                # Remove price if present (format: "Location|€ price")
                if '|' in location:
                    parts = location.split('|')
                    location = parts[0].strip()

        # Find estate-price
        estate_price_elem = container.find(class_=lambda x: x and 'estate-price' in str(x).lower())
        price_raw = estate_price_elem.get_text(strip=True) if estate_price_elem else ""

        # Find description (p with text-18, mb-10, leading-7)
        desc_elem = container.find('p', class_=lambda x: x and 'text-18' in str(x) and 'mb-10' in str(x) and 'leading-7' in str(x))
        description = desc_elem.get_text(strip=True) if desc_elem else ""

        # Find photo from anchor or container
        photo_url = self.find_photo(container)

        return {
            'listing_id': listing_id,
            'listing_url': listing_url,
            'listing_id_number': listing_id_number,
            'listing_type': listing_type,
            'location': location,
            'price_raw': price_raw,
            'description': description,
            'photo_candidate': photo_url,
            'container': container,
        }

    def find_photo(self, element):
        """Find the best photo URL within an element."""
        candidates = []

        # Check img tags
        for img in element.find_all('img'):
            for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'srcset', 'data-srcset'):
                v = img.get(attr)
                if v:
                    if 'srcset' in attr:
                        candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                    else:
                        candidates.append(v)

        # Check source tags
        for source in element.find_all('source'):
            for attr in ('srcset', 'data-srcset', 'src'):
                v = source.get(attr)
                if v:
                    if 'srcset' in attr:
                        candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                    else:
                        candidates.append(v)

        # Normalize candidates
        normed = []
        for c in candidates:
            if not c:
                continue
            c = normalize_url(c)
            if c and not c.startswith('data:') and 'svg' not in c.lower():
                normed.append(c)

        # Prefer uploads_c/siteassets URLs
        for n in normed:
            if '/uploads_c/siteassets/' in n:
                return n

        return normed[0] if normed else ""

    def format_price(self, raw):
        """Format price string."""
        if not raw:
            return ""
        s = normalize_text(raw)
        if re.search(r'Prijs op aanvraag', s, re.I):
            return "Prijs op aanvraag"
        if re.search(r'Compromis', s, re.I):
            return "Compromis in opmaak"
        if re.search(r'Vraag prijs', s, re.I):
            return "Vraag prijs aan"
        cleaned = s.replace('\u20ac', '').replace('€', '')
        cleaned = re.sub(r'(?i)prijs.*|compromis.*', '', cleaned).strip()
        digits = re.sub(r'[^0-9]', '', cleaned)
        if digits:
            try:
                num = int(digits)
                formatted = format(num, ',').replace(',', '.')
                return f"€ {formatted}"
            except Exception:
                pass
        return s

    def extract_detail_page_info(self, listing_url, listing_id):
        """Fetch and extract info from detail page."""
        time.sleep(0.1)  # Polite crawl delay
        try:
            response = secure_get(listing_url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(response.content, 'html.parser')
        except Exception as e:
            logger.warning(f"Failed to fetch {listing_url}: {e}")
            return {}

        info = {}

        # Extract price from <p> tags
        price_p = soup.find('p', string=re.compile(r'€|Prijs op aanvraag|Compromis|Vraag prijs', re.I))
        if price_p:
            info['price'] = normalize_text(price_p.get_text(strip=True))

        # Extract address
        address_lines = []
        for div in soup.find_all('div', class_=lambda x: x and 'lg:w-1/2' in str(x)):
            ps = div.find_all('p', recursive=False, limit=3)
            if len(ps) >= 2:
                second_p = normalize_text(ps[1].get_text(strip=True))
                if second_p and re.match(r'^\d{4}\s', second_p):
                    address_lines = [normalize_text(p.get_text(strip=True)) for p in ps[:2]]
                    break
        if address_lines:
            info['address'] = f"{address_lines[0]}, {address_lines[1]}"

        # Extract email from mailto links
        email = ""
        first_name = ""
        for a in soup.find_all('a', href=re.compile(r'^mailto:', re.I)):
            href = a.get('href', '')
            m = re.search(r'mailto:([^?]+)', href)
            if m:
                email = m.group(1).strip()
                if email:
                    local_part = email.split('@')[0]
                    parts = re.split(r'[._\-]', local_part)
                    if parts:
                        first_name = parts[0].capitalize()
                    break

        if email:
            info['email'] = email
            info['first_name'] = first_name

        # Extract page content from main element
        main = soup.find('main', attrs={'data-barba': True})
        if main:
            content = self.extract_main_content(main)
            info['page_content'] = content

        # Extract photo from detail page if needed
        photo = self.find_photo(main) if main else ""
        if photo:
            info['photo_url'] = photo

        return info

    def extract_main_content(self, main):
        """Extract all visible text from main content area."""
        parts = []

        # Extract all text content with structure
        for elem in main.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li']):
            text = normalize_text(elem.get_text(strip=True))
            if text:
                tag = elem.name
                if tag in ['h1', 'h2', 'h3', 'h4']:
                    prefix = '#' * (int(tag[1:]) + 1) if tag != 'h1' else '#'
                    parts.append(f"{prefix} {text}")
                elif tag == 'li':
                    parts.append(f"* {text}")
                else:
                    parts.append(text)

        # Extract feature sections with labels
        for section in main.find_all(['div', 'section']):
            for li in section.find_all('li'):
                # Check for label: value pattern
                strong = li.find(['strong', 'b', 'p'], class_=lambda x: x and ('font-bold' in str(x) or 'bold' in str(x)))
                if strong:
                    label = normalize_text(strong.get_text(strip=True))
                    value = normalize_text(li.get_text(strip=True))
                    if label and value and label != value:
                        parts.append(f"* {label}: {value}")

        # Extract links
        for a in main.find_all('a', href=True):
            href = a.get('href', '')
            text = normalize_text(a.get_text(strip=True))
            if href and text:
                parts.append(f"[{text}]({normalize_url(href)})")

        result = '\n'.join(parts)
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result.strip()

    def scrape(self):
        """Main scraping method."""
        try:
            html_content = self.fetch_page()
            soup = BeautifulSoup(html_content, 'html.parser')

            # Find all listing containers
            # Each listing is in a div with class containing project-hover
            containers = soup.find_all('div', class_=lambda x: x and 'project-hover' in str(x))
            logger.info(f"Found {len(containers)} listing containers")

            # Parse each container
            raw_listings = []
            for container in containers:
                parsed = self.parse_listing_card(container)
                if parsed:
                    raw_listings.append(parsed)

            # Deduplicate by listing_id
            seen_ids = set()
            unique_listings = []
            for lst in raw_listings:
                lid = lst.get('listing_id')
                if lid and lid not in seen_ids:
                    seen_ids.add(lid)
                    unique_listings.append(lst)

            logger.info(f"Found {len(unique_listings)} unique listings")

            # Fetch detail pages for additional info
            listings = []
            for lst in unique_listings:
                listing_id = lst.get('listing_id')
                listing_url = lst.get('listing_url')

                # Fetch detail page
                detail_info = self.extract_detail_page_info(listing_url, listing_id)

                # Build final listing object
                price_formatted = self.format_price(lst.get('price_raw'))
                if detail_info.get('price'):
                    price_formatted = self.format_price(detail_info['price'])

                # Title: {location}⎥{price}
                title = ""
                location = lst.get('location', '')
                if location and price_formatted:
                    title = f"{location}⎥{price_formatted}"
                elif location:
                    title = location
                elif price_formatted:
                    title = price_formatted

                # Photo URL: prefer detail page, then card
                photo_url = detail_info.get('photo_url', '') or lst.get('photo_candidate', '')
                if not photo_url:
                    photo_url = lst.get('photo_candidate', '')
                photo_url = normalize_url(photo_url)

                # Address
                address = detail_info.get('address', '')

                # Page content
                page_content = detail_info.get('page_content', '')

                # Email and contact info
                email = detail_info.get('email', '')
                first_name = detail_info.get('first_name', '')

                # Buttons
                button1_label = "Bekijk het op onze website"
                button1_value = f"{listing_url}?utm_source=habichat"

                button2_label = ""
                button2_value = ""
                if email:
                    local_part = email.split('@')[0]
                    name_parts = [p.capitalize() for p in re.split(r'[._\-]', local_part) if p]
                    name_label = " ".join(name_parts)
                    button2_label = f"Email {name_label} - Irres"
                    button2_value = f"mailto:{email}"

                # Button3: only for "Prijs op aanvraag" or "Vraag prijs aan"
                button3_label = ""
                button3_value = ""
                if price_formatted in ["Prijs op aanvraag", "Vraag prijs aan"]:
                    button3_label = "Vraag prijs aan"
                    if email:
                        button3_value = f"mailto:{email}?subject=Prijs aanvraag {listing_id}"

                # Ensure listing has the required ID number format
                listing_id_number = lst.get('listing_id_number', '')
                if not listing_id_number:
                    listing_id_number = extract_listing_id_from_url(listing_url)

                # Ensure photo_url follows required format
                if photo_url and '/uploads_c/siteassets/' not in photo_url:
                    # Try to find a better photo
                    pass

                listing_obj = {
                    "listing_id": listing_id,
                    "listing_url": listing_url,
                    "listing_type": lst.get('listing_type', ''),
                    "photo_url": photo_url,
                    "title": title,
                    "price": price_formatted,
                    "location": location,
                    "description": lst.get('description', ''),
                    "button1_label": button1_label,
                    "button1_value": button1_value,
                    "button2_label": button2_label,
                    "button2_value": button2_value,
                    "button3_label": button3_label,
                    "button3_value": button3_value,
                    "address": address,
                    "page_content": page_content,
                }

                # Only include if it has meaningful content
                if location or price_formatted or lst.get('description'):
                    listings.append(listing_obj)

            return {
                "success": True,
                "count": len(listings),
                "listings": listings,
            }

        except Exception as e:
            logger.error(f"ListingsScraper failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "count": 0,
                "listings": [],
            }


# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route('/listings', methods=['GET'])
@limiter.limit("15 per hour")
def get_listings():
    """Get all property listings."""
    try:
        logger.info("GET /listings — scraping listings")
        scraper = IRRESListingsScraper()
        result = scraper.scrape()
        return Response(
            json.dumps(result, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        )
    except Exception as e:
        logger.error(f"GET /listings failed: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "count": 0,
            "listings": [],
        }), 500


@app.route('/locations', methods=['GET'])
@limiter.limit("15 per hour")
def get_locations():
    """Get all property locations."""
    try:
        logger.info("GET /locations — fetching locations")
        scraper = IRRESLocationScraper()
        result = scraper.scrape()

        # Format output as per spec
        output = {
            "status": result['status'],
            "count": result['count'],
            "locations": result['all_locations'],
        }
        if result['status'] == 'error':
            output['error'] = result.get('error', '')

        return Response(
            json.dumps(output, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        )
    except Exception as e:
        logger.error(f"GET /locations failed: {e}")
        return jsonify({
            "status": "error",
            "count": 0,
            "locations": [],
            "error": str(e),
        }), 500


@app.route('/office-images', methods=['GET'])
@limiter.limit("15 per hour")
def get_office_images():
    """Get all office images."""
    try:
        logger.info("GET /office-images — fetching office images")
        scraper = IRRESOfficeImagesScraper()
        result = scraper.scrape()

        return Response(
            json.dumps(result, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        )
    except Exception as e:
        logger.error(f"GET /office-images failed: {e}")
        return jsonify({
            "status": "error",
            "offices": [],
            "count": 0,
            "error": str(e),
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "IRRES Scraper API",
    })


@app.route('/', methods=['GET'])
def root():
    """API documentation."""
    return jsonify({
        "api": "IRRES.be Scraper API",
        "version": "1.0",
        "endpoints": {
            "/listings": "Get all property listings with full details",
            "/locations": "Get all available property locations",
            "/office-images": "Get all office images",
            "/health": "Health check endpoint",
        },
        "authentication": "X-API-KEY header required on all endpoints except /health",
        "rate_limits": "15 requests per hour per IP on scraping endpoints",
    })


# =============================================================================
# RUN SERVER
# =============================================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
