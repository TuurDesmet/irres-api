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
#   BLOCK 7  — Class: IRRESOfficeImagesScraper
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
#   BLOCK 19 — API Endpoint: /api/listings (UPDATED WITH PAGINATION)
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
app.config['JSON_AS_ASCII'] = False


# =============================================================================
# BLOCK 2 — RATE LIMITING CONFIGURATION
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
# BLOCK 3 — SECURITY & AUTHENTICATION
# =============================================================================

API_KEY = os.getenv("API_KEY")
if API_KEY is None:
    # Use a dummy key for testing if not set, but warn the user.
    API_KEY = "DEV_MODE_KEY_CHANGE_ME"

@app.before_request
def require_api_key():
    if request.endpoint == 'static':
        return

    if 'api_key' in request.args:
        return jsonify({
            "error": "Unauthorized",
            "message": "API key must be provided via X-API-KEY header, not query parameters"
        }), 401

    provided_api_key = request.headers.get('X-API-KEY')

    if not provided_api_key or provided_api_key != API_KEY:
        return jsonify({
            "error": "Unauthorized",
            "message": "Invalid or missing X-API-KEY header"
        }), 401

    return None

@app.errorhandler(429)
def ratelimit_handler(e):
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

TYPE_MAPPING = {
    'Dwelling': 'Huis',
    'Flat':     'Appartement',
    'Land':     'Grond',
    'dwelling': 'Huis',
    'flat':     'Appartement',
    'land':     'Grond',
}

def secure_get(url, headers=None, timeout=15):
    if not url.startswith('https://'):
        if url.startswith('http://'):
            url = url.replace('http://', 'https://', 1)
        else:
            if not url.startswith('irres.be'):
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


# =============================================================================
# BLOCK 6 — CLASS: IRRESLocationScraper
# =============================================================================

class IRRESLocationScraper:
    BASE_URL = "https://irres.be/te-koop"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.all_locations  = []
        self.location_groups = {}

    @staticmethod
    def normalize_text(text: str) -> str:
        nfd = unicodedata.normalize('NFD', text)
        return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')

    def fetch_page(self) -> str:
        try:
            response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
            return response.text
        except requests.RequestException as e:
            logger.error(f"Failed to fetch locations page: {e}")
            raise

    def parse_locations(self, html_content: str):
        soup = BeautifulSoup(html_content, 'html.parser')
        all_locations  = []
        location_groups = {}

        filter_container = soup.find(
            'div',
            attrs={
                'class': lambda c: c and 'filter-container' in c,
                'data-category': 'city'
            }
        )
        if not filter_container:
            filter_container = soup

        search_data_ul = filter_container.find('ul', class_='search-data')
        if not search_data_ul:
            return all_locations, location_groups

        li_elements = search_data_ul.find_all('li', attrs={'data-label': True, 'data-value': True})
        
        NON_LOCATION_TYPES = {
            'Huis', 'Appartement', 'Grond', 'Kantoor', 'Garage', 'Parking',
            'Opbrengsteigendom', 'Handelspand', 'Industrieel', 'Commercieel', 'Project',
        }
        MAPPED_TYPES = set(TYPE_MAPPING.values())

        seen_labels: dict = {}

        for li in li_elements:
            label = li.get('data-label', '').strip()
            value = li.get('data-value', '').strip()

            if not label or not value or '€' in label:
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

        return all_locations, location_groups

    def scrape(self) -> dict:
        try:
            html_content = self.fetch_page()
            all_locations, location_groups = self.parse_locations(html_content)
            self.all_locations   = all_locations
            self.location_groups = location_groups

            status = "success" if all_locations else "warning"
            return {
                "all_locations":   all_locations,
                "location_groups": location_groups,
                "count":           len(all_locations),
                "status":          status,
            }
        except Exception as e:
            return {
                "all_locations": [], "location_groups": {},
                "count": 0, "status": "error", "error": str(e),
            }


# =============================================================================
# BLOCK 7 — CLASS: IRRESOfficeImagesScraper
# =============================================================================

class IRRESOfficeImagesScraper:
    BASE_URL = "https://irres.be/contact"
    OFFICE_ID_MAP = [
        ('IrresGentImage',         ['gent']),
        ('IrresLatemImage',        ['sint-martens-latem', 'latem', 'sml']),
        ('IrresDestelbergenImage', ['destelbergen']),
    ]

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def fetch_page(self) -> str:
        response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
        return response.text

    @staticmethod
    def extract_image_url_from_section(section) -> str:
        def parse_srcset(srcset_value: str) -> str:
            if not srcset_value: return ''
            return srcset_value.split(',')[0].strip().split(' ')[0].strip()

        def make_absolute(url: str) -> str:
            if not url or url.startswith('data:'): return ''
            if url.startswith('http'): return url
            return ('https://irres.be' + url) if url.startswith('/') else f'https://irres.be/{url}'

        for img in section.find_all('img'):
            srcset = img.get('srcset', '')
            if srcset and not srcset.startswith('data:'):
                url = make_absolute(parse_srcset(srcset))
                if url: return url

        for img in section.find_all('img'):
            for attr in ('src', 'data-src', 'data-lazy-src', 'data-original'):
                val = img.get(attr, '')
                if val and not val.startswith('data:'):
                    url = make_absolute(val)
                    if url: return url
        return ''

    def parse_office_images(self, html_content: str) -> dict:
        soup   = BeautifulSoup(html_content, 'html.parser')
        images = {}

        for result_key, id_candidates in self.OFFICE_ID_MAP:
            section = None
            for section_id in id_candidates:
                section = soup.find(id=section_id)
                if section: break

            if not section: continue
            image_url = self.extract_image_url_from_section(section)
            if image_url: images[result_key] = image_url
        return images

    def scrape(self) -> dict:
        try:
            html_content = self.fetch_page()
            images       = self.parse_office_images(html_content)
            return {"status": "success", "images": images, "count": len(images)}
        except Exception as e:
            return {"status": "error", "images": {}, "count": 0, "error": str(e)}


# =============================================================================
# BLOCK 8-10 — LISTING HELPERS (TEXT & URLs)
# =============================================================================

def normalize_text(s):
    if s is None: return ""
    try: s = str(s)
    except: return ""
    s = html.unescape(s)
    if "\\u" in s or "\\x" in s:
        try: s = bytes(s, "utf-8").decode("unicode_escape")
        except: pass
    s = " ".join(s.split())
    return s.strip()

def normalize_url(src, add_tracking=False):
    if not src: return ""
    src = src.strip().strip('\"\'')

    if src.startswith("//"): url = "https:" + src
    elif src.startswith("/"): url = "https://irres.be" + src
    elif re.match(r'https?://', src, re.I): url = src
    elif src.startswith("www."): url = "https://" + src
    elif not re.search(r':', src): url = "https://irres.be/" + src.lstrip('/')
    else: url = src

    if add_tracking and '/pand/' in url:
        separator = '&' if '?' in url else '?'
        url = f"{url}{separator}origin=habichat"
    return url

def extract_listing_id_from_url(url):
    if not url: return ""
    m = re.search(r'/pand/(\d+)', url)
    return m.group(1) if m else ""


# =============================================================================
# BLOCK 11-13 — LISTING HELPERS (PRICING & CARDS)
# =============================================================================

def format_price_string(raw):
    if not raw: return ""
    s = normalize_text(raw)
    if re.search(r'Prijs op aanvraag', s, re.I): return "Prijs op aanvraag"
    if re.search(r'Compromis', s, re.I): return "Compromis in opmaak"

    cleaned = s.replace('€', '').replace('\u20ac', '')
    cleaned = re.sub(r'(?i)prijs op aanvraag|compromis.*', '', cleaned).strip()
    digits  = re.sub(r'[^0-9]', '', cleaned)
    if not digits: return s

    try:
        num = int(digits)
        formatted = format(num, ',').replace(',', '.')
        return f"€ {formatted}"
    except: return s

def parse_main_listing_card(link):
    href = normalize_text(link.get('href') or "")
    if href and not href.startswith('http'):
        href = normalize_url(href, add_tracking=True)
    elif href.startswith('http') and '/pand/' in href:
        separator = '&' if '?' in href else '?'
        href = f"{href}{separator}origin=habichat"

    anchor_name = normalize_text(link.get('name') or link.get('data-name') or "")
    location  = ""
    city_h2   = link.find('h2', class_=re.compile(r'estate-city'))
    if city_h2:
        location = city_h2.get('data-value', '').strip()
        if not location: location = normalize_text(city_h2.get_text())

    text  = link.get_text(separator="|", strip=True)
    text  = normalize_text(text)
    parts = [normalize_text(x) for x in text.split("|") if normalize_text(x)]

    price, description, listing_type = "", "", ""
    for p in parts:
        if '€' in p or re.search(r'Prijs op aanvraag', p, re.I) or re.search(r'Compromis', p, re.I):
            price = p
            continue
        if p in TYPE_MAPPING or p in TYPE_MAPPING.values() or p.lower() in TYPE_MAPPING:
            listing_type = TYPE_MAPPING.get(p, TYPE_MAPPING.get(p.lower(), p))
            continue
        if not description and p != location and p != listing_type:
            description = p

    if not description and len(parts) >= 2:
        possible = parts[-1]
        if possible not in TYPE_MAPPING and '€' not in possible and possible != location:
            description = possible

    photo_url = find_photo_on_element(link)
    return {
        "listing_url": href, "location": location, "price_raw": price,
        "description": description, "listing_type": listing_type,
        "photo_candidate": photo_url, "anchor_name": anchor_name,
    }

def find_photo_on_element(el):
    candidates = []
    for img in el.find_all('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'srcset', 'data-srcset'):
            v = img.get(attr)
            if v:
                if 'srcset' in attr: candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                else: candidates.append(v)
    for node in ([el] + el.find_all(True)):
        style = node.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m: candidates.append(m.group(1))

    normed = []
    for c in candidates:
        if not c: continue
        c = normalize_url(c)
        if c and not c.startswith('data:') and 'svg' not in c.lower(): normed.append(c)

    for n in normed:
        if re.search(r'/uploads|uploads_c|/siteassets|/panden', n, re.I) or \
           re.search(r'\.(jpg|jpeg|png|webp|gif)(?:\?|$)', n, re.I): return n
    return normed[0] if normed else ""


# =============================================================================
# BLOCK 14-18 — DETAIL PAGE HELPERS
# =============================================================================

def find_landscape_image_from_detail(soup):
    main = soup.find('main', attrs={'data-barba': True, 'data-barba-namespace': True})
    search_scope = main if main else soup
    candidates = []
    for img in search_scope.find_all('img'):
        v = img.get('src') or img.get('data-src')
        if v: candidates.append(v)
    
    for c in candidates:
        c = normalize_url(c)
        if c and not c.startswith('data:'): return c
    return ""

def extract_address_from_detail_soup(soup):
    try:
        containers = soup.find_all('div', class_=re.compile(r'lg:w-1/2'))
        for container in containers:
            paragraphs = container.find_all('p', recursive=False, limit=3)
            if len(paragraphs) >= 2:
                first_p = normalize_text(paragraphs[0].get_text())
                second_p = normalize_text(paragraphs[1].get_text())
                if second_p and re.match(r'^\d{4}\s', second_p):
                    return f"{first_p}\n{second_p}"
        return ""
    except: return ""

def extract_page_content_from_detail_soup(soup):
    try:
        main = soup.find('main', attrs={'data-barba': True}) or soup.find('main') or soup
        content_parts = []
        body_sections = main.find_all('div', class_=re.compile(r'body|text-18'))
        for section in body_sections:
            for p in section.find_all('p', recursive=True):
                p_text = normalize_text(p.get_text())
                if p_text and len(p_text) > 10:
                    content_parts.append(p_text)
        return '\n\n'.join(content_parts).strip()
    except: return ""

def extract_contact_and_email_from_detail(soup):
    email, first_name = "", ""
    all_mailto = soup.find_all('a', href=re.compile(r'^mailto:', re.I))
    for a in all_mailto:
        href = a.get('href', '')
        m = re.search(r'mailto:([^?]+)', href)
        if m:
            email = normalize_text(m.group(1))
            break
    if email:
        local = email.split('@')[0]
        local_token = re.split(r'[._\-]', local)[0]
        if local_token: first_name = local_token.capitalize()
    return first_name, email

def fetch_detail_page(url, timeout=12):
    try:
        response = secure_get(url, headers=HEADERS, timeout=timeout)
        return BeautifulSoup(response.content, 'html.parser')
    except: return None


# =============================================================================
# BLOCK 19 — API ENDPOINT: /api/listings (FIXED WITH PAGINATION)
# =============================================================================

@limiter.limit("15 per hour")
@app.route('/api/listings', methods=['GET'])
def get_listings():
    """
    Scrape all active property listings from IRRES.be/te-koop across ALL pages.
    Now properly uses a while loop to navigate pagination.
    """
    try:
        page = 1
        seen_links = set()
        listing_links = []
        
        # 1. Fetch all listing cards across all pagination pages
        while True:
            list_page_url = f"https://irres.be/te-koop?page={page}" if page > 1 else "https://irres.be/te-koop"
            logger.info(f"Scraping overview page {page}: {list_page_url}")
            
            resp = secure_get(list_page_url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.content, 'html.parser')

            anchors = soup.find_all('a', href=re.compile(r'/pand/\d+/', re.I))
            new_on_this_page = 0

            for a in anchors:
                href = a.get('href') or ""
                if not href: continue

                full = normalize_url(href, add_tracking=True) if not href.startswith('http') else href
                if full.startswith('http') and '/pand/' in full:
                    separator = '&' if '?' in full else '?'
                    full = f"{full}{separator}origin=habichat"

                if full not in seen_links:
                    text = a.get_text(separator="|", strip=True)
                    if text and text.strip():
                        seen_links.add(full)
                        listing_links.append(a)
                        new_on_this_page += 1

            # Break loop if we hit a page with no new unique properties
            if new_on_this_page == 0:
                logger.info(f"Pagination complete. Reached end at page {page}.")
                break
                
            page += 1
            time.sleep(0.5) # Polite scraping delay between pages

        # 2. Parse details for each collected listing
        listings = []
        for link in listing_links:
            parsed = parse_main_listing_card(link)
            listing_url = parsed['listing_url'] or ""
            if not listing_url: continue

            listing_id_num  = extract_listing_id_from_url(listing_url)
            parsed_location = parsed.get('location') or ""
            lt = parsed['listing_type'] or ""
            lt_mapped = TYPE_MAPPING.get(lt, TYPE_MAPPING.get(lt.lower(), lt)) if lt else ""
            photo_url = parsed['photo_candidate'] or ""

            time.sleep(0.09) 
            detail_soup = fetch_detail_page(listing_url)

            button2_label, button2_email, address, page_content = "", "", "", ""
            if detail_soup:
                first_name, email = extract_contact_and_email_from_detail(detail_soup)
                if email:
                    button2_email = f"mailto:{email}"
                    name_label = first_name if first_name else email.split('@')[0]
                    name_label = " ".join([p.capitalize() for p in re.split(r'[._\-]', name_label) if p])
                    button2_label = f"Contacteer {name_label} - Irres"

                address = extract_address_from_detail_soup(detail_soup)
                page_content = extract_page_content_from_detail_soup(detail_soup)
                if not photo_url: photo_url = find_landscape_image_from_detail(detail_soup)

            photo_url = normalize_url(photo_url) if photo_url else ""
            price_formatted = format_price_string(parsed['price_raw']) if parsed['price_raw'] else ""

            Title = f"{parsed_location}⎥{price_formatted}" if parsed_location and price_formatted else (parsed_location or price_formatted)
            if (not parsed_location) and Title and '⎥' in Title:
                possible_loc = Title.split('⎥', 1)[0].strip()
                if possible_loc and '€' not in possible_loc: parsed_location = normalize_text(possible_loc)

            anchor_name = parsed.get('anchor_name') or ""
            if anchor_name: listing_id = anchor_name
            else:
                location_for_id = re.sub(r'[^A-Za-z0-9\-]', '', (parsed_location.split()[0] if parsed_location else ""))
                listing_id = f"{listing_id_num}-{location_for_id}" if listing_id_num else listing_url

            button3_label = "Vraag prijs aan" if price_formatted == "Prijs op aanvraag" else ""
            button3_value = f"{button2_email}?subject=Prijs aanvraag {listing_id}" if price_formatted == "Prijs op aanvraag" else ""

            listing_obj = {
                "listing_id":    listing_id,
                "listing_url":   listing_url,
                "photo_url":     photo_url,
                "price":         price_formatted,
                "location":      parsed_location,
                "description":   parsed.get('description') or "",
                "listing_type":  lt_mapped,
                "address":       address,
                "page_content":  page_content,
                "Title":         Title,
                "Button1_Label": "Bekijk het op onze website",
                "Button2_Label": button2_label,
                "Button2_email": button2_email,
                "Button3_Label": button3_label,
                "Button3_Value": button3_value,
            }

            if listing_obj.get("location") or listing_obj.get("price") or listing_obj.get("description"):
                listings.append(listing_obj)

        # Deduplicate
        uniq, seen_ids = [], set()
        for li in listings:
            lid = li.get("listing_id")
            if lid in seen_ids: continue
            seen_ids.add(lid)
            uniq.append(li)

        return Response(
            json.dumps({"success": True, "count": len(uniq), "listings": uniq}, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        )

    except Exception as e:
        return Response(
            json.dumps({"success": False, "error": str(e), "listings": []}, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        ), 200


# =============================================================================
# BLOCK 20-22 — LOCATIONS, IMAGES & HEALTH
# =============================================================================

@limiter.limit("15 per hour")
@app.route('/api/locations', methods=['GET'])
def get_locations():
    try:
        scraper = IRRESLocationScraper()
        result  = scraper.scrape()
        if request.args.get('format', 'json').lower() == 'csv':
            csv_content = "location\n" + "\n".join([loc['label'] for loc in result['all_locations']])
            return Response(response=csv_content, status=200, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=irres_locations.csv"})
        return jsonify({"status": "success", "timestamp": datetime.now().isoformat(), "data": {"all_locations": result['all_locations'], "location_groups": result['location_groups'], "count": len(result['all_locations'])}}), 200
    except Exception as e:
        return jsonify({"status": "error", "timestamp": datetime.now().isoformat(), "message": str(e)}), 500

@limiter.limit("15 per hour")
@app.route('/api/office-images', methods=['GET'])
def get_office_images():
    try:
        scraper = IRRESOfficeImagesScraper()
        result  = scraper.scrape()
        code = 200 if result['status'] == 'success' else 500
        return jsonify({"status": result['status'], "timestamp": datetime.now().isoformat(), "data": result['images']}), code
    except Exception as e:
        return jsonify({"status": "error", "timestamp": datetime.now().isoformat(), "message": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat(), "service": "IRRES Unified Scraper"})

@app.route('/', methods=['GET'])
def root():
    return jsonify({"api": "IRRES.be Unified Scraper", "version": "8.1", "status": "Online"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
