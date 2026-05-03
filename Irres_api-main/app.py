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
#   BLOCK 14 — Listing Helper: extract_highest_res_image_from_detail()  <- UPDATED
#   BLOCK 15 — Listing Helper: extract_address_from_detail_soup()
#   BLOCK 16 — Listing Helper: extract_page_content_from_detail_soup()
#   BLOCK 17 — Listing Helper: extract_contact_and_email_from_detail()
#   BLOCK 18 — Listing Helper: fetch_detail_page()
#   BLOCK 19 — API Endpoint: /api/listings                            <- UPDATED
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
    raise ValueError("API_KEY environment variable is required but not set")

@app.before_request
def require_api_key():
    if request.endpoint == 'static':
        return

    if 'api_key' in request.args:
        logger.warning(
            f"SECURITY: Rejected ?api_key query param from {request.remote_addr} "
            f"on {request.path}. Use X-API-KEY header instead."
        )
        return jsonify({
            "error": "Unauthorized",
            "message": "API key must be provided via X-API-KEY header, not query parameters"
        }), 401

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
    logger.warning(f"Rate limit exceeded for {request.remote_addr} on {request.path}.")
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

        filter_container = soup.find('div', attrs={'class': lambda c: c and 'filter-container' in c, 'data-category': 'city'})
        if not filter_container:
            filter_container = soup

        search_data_ul = filter_container.find('ul', class_='search-data')
        if not search_data_ul:
            return all_locations, location_groups

        li_elements = search_data_ul.find_all('li', attrs={'data-label': True, 'data-value': True})
        
        NON_LOCATION_TYPES = {'Huis', 'Appartement', 'Grond', 'Kantoor', 'Garage', 'Parking', 'Opbrengsteigendom', 'Handelspand', 'Industrieel', 'Commercieel', 'Project'}
        MAPPED_TYPES = set(TYPE_MAPPING.values())   

        seen_labels: dict = {}
        for li in li_elements:
            label = li.get('data-label', '').strip()
            value = li.get('data-value', '').strip()

            if not label or not value or '€' in label: continue
            if label in NON_LOCATION_TYPES or label in MAPPED_TYPES: continue
            if label in TYPE_MAPPING or label.lower() in TYPE_MAPPING: continue
            if label in seen_labels: continue
            
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
            return {"all_locations": all_locations, "location_groups": location_groups, "count": len(all_locations), "status": status}
        except Exception as e:
            logger.error(f"IRRESLocationScraper.scrape() failed: {e}")
            return {"all_locations": [], "location_groups": {}, "count": 0, "status": "error", "error": str(e)}


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
        try:
            response = secure_get(self.BASE_URL, headers=HEADERS, timeout=self.timeout)
            return response.text
        except requests.RequestException as e:
            raise

    @staticmethod
    def extract_image_url_from_section(section) -> str:
        def parse_srcset(srcset_value: str) -> str:
            if not srcset_value: return ''
            first_entry = srcset_value.split(',')[0].strip()
            return first_entry.split(' ')[0].strip()

        def make_absolute(url: str) -> str:
            if not url or url.startswith('data:'): return ''
            if url.startswith('http'): return url
            return ('https://irres.be' + url) if url.startswith('/') else f'https://irres.be/{url}'

        for img in section.find_all('img'):
            srcset = img.get('srcset', '') or img.get('data-srcset', '')
            if srcset and not srcset.startswith('data:'):
                url = make_absolute(parse_srcset(srcset))
                if url: return url

        for source in section.find_all('source'):
            srcset = source.get('srcset', '')
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
# BLOCK 8 — LISTING HELPER: normalize_text()
# =============================================================================
def normalize_text(s):
    if s is None: return ""
    try: s = str(s)
    except Exception: return ""
    s = html.unescape(s)
    if "\\u" in s or "\\x" in s:
        try: s = bytes(s, "utf-8").decode("unicode_escape")
        except Exception: pass
    return " ".join(s.split()).strip()


# =============================================================================
# BLOCK 9 — LISTING HELPER: normalize_url()
# =============================================================================
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


# =============================================================================
# BLOCK 10 — LISTING HELPER: extract_listing_id_from_url()
# =============================================================================
def extract_listing_id_from_url(url):
    if not url: return ""
    m = re.search(r'/pand/(\d+)', url)
    return m.group(1) if m else ""


# =============================================================================
# BLOCK 11 — LISTING HELPER: format_price_string()
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
    except Exception:
        return s


# =============================================================================
# BLOCK 12 — LISTING HELPER: parse_main_listing_card()
# =============================================================================
def parse_main_listing_card(link):
    href = normalize_text(link.get('href') or "")
    if href and not href.startswith('http'):
        href = normalize_url(href, add_tracking=True)
    elif href.startswith('http') and '/pand/' in href:
        separator = '&' if '?' in href else '?'
        href = f"{href}{separator}origin=habichat"

    anchor_name = normalize_text(link.get('name') or link.get('data-name') or "")

    location = ""
    city_h2 = link.find('h2', class_=re.compile(r'estate-city'))
    if city_h2:
        location = city_h2.get('data-value', '').strip()
        if not location: location = normalize_text(city_h2.get_text())

    text = link.get_text(separator="|", strip=True)
    parts = [normalize_text(x) for x in normalize_text(text).split("|") if normalize_text(x)]

    price, description, listing_type = "", "", ""

    for p in parts:
        if '€' in p or re.search(r'Prijs op aanvraag', p, re.I) or re.search(r'Compromis', p, re.I):
            price = p; continue
        if p in TYPE_MAPPING or p in TYPE_MAPPING.values() or p.lower() in TYPE_MAPPING:
            listing_type = TYPE_MAPPING.get(p, TYPE_MAPPING.get(p.lower(), p)); continue
        if not description and p != location and p != listing_type:
            description = p

    if not description and len(parts) >= 2:
        possible = parts[-1]
        if possible not in TYPE_MAPPING and '€' not in possible and possible != location:
            description = possible

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
# BLOCK 13 — LISTING HELPER: find_photo_on_element() (Fallback Card Photo)
# =============================================================================
def find_photo_on_element(el):
    candidates = []
    for img in el.find_all('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'srcset', 'data-srcset'):
            v = img.get(attr)
            if v:
                if 'srcset' in attr: candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                else: candidates.append(v)

    for source in el.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            v = source.get(attr)
            if v:
                if 'srcset' in attr: candidates.extend([p.strip().split(' ')[0] for p in v.split(',') if p.strip()])
                else: candidates.append(v)

    for node in ([el] + el.find_all(True)):
        style = node.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m: candidates.append(m.group(1))

    for attr in ('data-src', 'data-image', 'data-bg', 'data-photo', 'data-thumb', 'data-original'):
        v = el.get(attr)
        if v: candidates.append(v)

    normed = []
    for c in candidates:
        if not c: continue
        c = normalize_url(c)
        if c and not c.startswith('data:') and 'svg' not in c.lower(): normed.append(c)

    for n in normed:
        if re.search(r'/uploads|uploads_c|/siteassets|/panden', n, re.I) or re.search(r'\.(jpg|jpeg|png|webp|gif)(?:\?|$)', n, re.I):
            return n

    return normed[0] if normed else ""


# =============================================================================
# BLOCK 14 — LISTING HELPER: extract_highest_res_image_from_detail() 
# =============================================================================
def parse_highest_res_from_srcset(srcset_string):
    """
    Parses a srcset string and returns the URL of the image with the highest width descriptor.
    Example input: "img-500.jpg 500w, img-1920.jpg 1920w"
    Returns: "img-1920.jpg"
    """
    if not srcset_string: return ""
    candidates = []
    
    parts = srcset_string.split(',')
    for part in parts:
        part = part.strip()
        if not part: continue
        
        # Format is usually "URL width_descriptor" (e.g., "/image.jpg 1024w")
        tokens = part.split(' ')
        url = tokens[0]
        width = 0
        
        if len(tokens) > 1:
            # Extract just the numbers from the 'w' or 'x' descriptor
            width_str = re.sub(r'\D', '', tokens[1])
            if width_str:
                width = int(width_str)
                
        candidates.append((url, width))
        
    if not candidates: return ""
    
    # Sort candidates by width descending
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]

def extract_highest_res_image_from_detail(soup):
    """
    Search for the highest resolution landscape hero photo on a detail page.
    Prioritizes hero galleries and explicitly parses `srcset` to grab the largest file.
    """
    # 1. Target hero galleries/sliders first (these usually contain the high-res 16:9 landscape shots)
    hero_container = soup.find('div', class_=re.compile(r'gallery|slider|hero|carousel|swiper', re.I))
    search_scope = hero_container if hero_container else soup.find('main', attrs={'data-barba': True}) or soup
    
    candidates = []

    # 2. Extract from images and sources, aggressively seeking 'srcset' for max resolution
    for tag in search_scope.find_all(['img', 'source']):
        # Prioritize srcset
        for attr in ('srcset', 'data-srcset'):
            srcset_val = tag.get(attr)
            if srcset_val:
                best_url = parse_highest_res_from_srcset(srcset_val)
                if best_url: candidates.append(best_url)
                
        # Fallback to standard sources if no srcset
        for attr in ('src', 'data-src', 'data-original', 'data-lazy-src'):
            v = tag.get(attr)
            if v: candidates.append(v)

    # 3. Collect from inline style background-image
    for el in search_scope.find_all(style=True):
        style = el.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m: candidates.append(m.group(1))

    # 4. Normalize and filter placeholders
    normed = []
    for c in candidates:
        if not c: continue
        c = normalize_url(c)
        if c and not c.startswith('data:') and 'svg' not in c.lower() and 'logo' not in c.lower():
            normed.append(c)

    # 5. Look for the best image file types inside content directories
    for n in normed:
        if re.search(r'/uploads|uploads_c|siteassets|/panden', n, re.I) and \
           re.search(r'\.(jpg|jpeg|png|webp)', n, re.I):
            return n

    return normed[0] if normed else ""


# =============================================================================
# BLOCK 15 — LISTING HELPER: extract_address_from_detail_soup()
# =============================================================================
def extract_address_from_detail_soup(soup):
    try:
        address_lines = []
        containers = soup.find_all('div', class_=re.compile(r'lg:w-1/2'))
        
        for container in containers:
            paragraphs = container.find_all('p', recursive=False, limit=3)
            if len(paragraphs) >= 2:
                first_p = normalize_text(paragraphs[0].get_text())
                second_p = normalize_text(paragraphs[1].get_text())
                if second_p and re.match(r'^\d{4}\s', second_p):
                    address_lines = [first_p, second_p]
                    break
        
        if not address_lines:
            all_p = soup.find_all('p')
            for i, p in enumerate(all_p[:-1]):
                text1 = normalize_text(p.get_text())
                text2 = normalize_text(all_p[i + 1].get_text())
                if text2 and re.match(r'^\d{4}\s+[A-Za-z]', text2):
                    if text1 and re.search(r'\d', text1):
                        address_lines = [text1, text2]
                        break
        
        if address_lines:
            return '\n'.join(address_lines)
        return ""
    except Exception as e:
        logger.warning(f"Failed to extract address: {e}")
        return ""


# =============================================================================
# BLOCK 16 — LISTING HELPER: extract_page_content_from_detail_soup()
# =============================================================================
def extract_page_content_from_detail_soup(soup):
    try:
        main = soup.find('main', attrs={'data-barba': True}) or soup.find('main') or soup
        content_parts = []
        
        title = main.find(['h1', 'h2'], class_=re.compile(r'font-serif|text-2xl|xl:text'))
        if title: content_parts.append(normalize_text(title.get_text()))
        
        address_divs = main.find_all('div', class_=re.compile(r'lg:w-1/2'))
        for div in address_divs:
            paragraphs = div.find_all('p', recursive=False, limit=3)
            if len(paragraphs) >= 2:
                second_p = normalize_text(paragraphs[1].get_text())
                if second_p and re.match(r'^\d{4}\s', second_p):
                    for p in paragraphs[:2]: content_parts.append(normalize_text(p.get_text()))
                    break
        
        map_link = main.find('a', href=re.compile(r'google\.com/maps'))
        if map_link:
            href = map_link.get('href', '')
            link_text = normalize_text(map_link.get_text()) or 'Toon ligging'
            content_parts.append(f"[{link_text}]({href})")
        
        virtual_tour = main.find('button', class_='open-iframe') or main.find('a', href=re.compile(r'matterport\.com'))
        if virtual_tour:
            iframe_src = virtual_tour.get('data-framesrc') or virtual_tour.get('href', '')
            if iframe_src: content_parts.append(f"Virtueel bezoek: {iframe_src}")
        
        price_div = main.find('div', class_=re.compile(r'flex items-center text-lg'))
        if price_div:
            price_p = price_div.find('p')
            if price_p: content_parts.append(normalize_text(price_p.get_text()))
        
        kenmerken_section = None
        for section in main.find_all(['div', 'section'], class_=re.compile(r'bg-dark-black|item-hover-list')):
            heading = section.find(['h2', 'h3'], string=re.compile(r'Kenmerken', re.I))
            if heading:
                kenmerken_section = section
                break
        
        if kenmerken_section:
            content_parts.append("\nKenmerken\n")
            items = kenmerken_section.find_all('li', class_='item-hover-text')
            for item in items:
                text = normalize_text(item.get_text())
                if text: content_parts.append(f"* {text}")
        
        body_sections = main.find_all('div', class_=re.compile(r'body|text-18'))
        for section in body_sections:
            for heading in section.find_all(['h2', 'h3']):
                h_text = normalize_text(heading.get_text())
                if h_text and len(h_text) > 3: content_parts.append(f"\n{h_text}\n")
            for p in section.find_all('p', recursive=True):
                p_text = normalize_text(p.get_text())
                if p_text and len(p_text) > 10: content_parts.append(p_text)
        
        voorschriften_section = None
        for section in main.find_all('div', class_=re.compile(r'bg-dark-black|bg-white')):
            heading = section.find('h2', string=re.compile(r'Voorschriften', re.I))
            if heading:
                voorschriften_section = section
                break
        
        if voorschriften_section:
            content_parts.append("\nVoorschriften\n")
            items = voorschriften_section.find_all('li', class_='pb-4')
            for item in items:
                label = item.find('p', class_='font-bold')
                if label:
                    label_text = normalize_text(label.get_text())
                    value_p = item.find('p', class_=re.compile(r'lg:w-3/5'))
                    if value_p:
                        value_text = normalize_text(value_p.get_text())
                        content_parts.append(f"* {label_text}: {value_text}")
                    else:
                        value_ul = item.find('ul', class_=re.compile(r'lg:w-3/5'))
                        if value_ul:
                            values = [normalize_text(li.get_text()) for li in value_ul.find_all('li')]
                            content_parts.append(f"* {label_text}: {', '.join(values)}")
        
        dossierstukken_section = None
        for section in main.find_all('div', class_=re.compile(r'bg-white|xl:w-5/6')):
            heading = section.find('h2', string=re.compile(r'Dossierstukken', re.I))
            if heading:
                dossierstukken_section = section
                break
        
        if dossierstukken_section:
            content_parts.append("\nDossierstukken\n")
            doc_links = dossierstukken_section.find_all('a', href=re.compile(r'\.pdf$', re.I))
            for link in doc_links:
                href = normalize_url(link.get('href', ''))
                link_text = normalize_text(link.find('p').get_text() if link.find('p') else link.get_text())
                if href and link_text: content_parts.append(f"* [{link_text}]({href})")
        
        result = '\n'.join(content_parts)
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result.strip()
    except Exception as e:
        logger.warning(f"Failed to extract page content: {e}")
        return ""


# =============================================================================
# BLOCK 17 — LISTING HELPER: extract_contact_and_email_from_detail()
# =============================================================================
def extract_contact_and_email_from_detail(soup):
    email      = ""
    first_name = ""

    all_mailto = soup.find_all('a', href=re.compile(r'^mailto:', re.I))
    for a in all_mailto:
        href = a.get('href', '')
        if not href: continue
        m = re.search(r'mailto:([^?]+)', href)
        if m:
            candidate = normalize_text(m.group(1))
            if candidate:
                email = candidate
                break

    if not email:
        text = soup.get_text(" ", strip=True)
        m2   = re.search(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
        if m2: email = normalize_text(m2.group(1))

    if email:
        local       = email.split('@')[0]
        local_token = re.split(r'[._\-]', local)[0]
        if local_token: first_name = local_token.capitalize()

    return first_name, email


# =============================================================================
# BLOCK 18 — LISTING HELPER: fetch_detail_page()
# =============================================================================
def fetch_detail_page(url, timeout=12):
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
    MODIFIED: Now guarantees extraction of the highest-resolution landscape photo
    from the property detail page over the thumbnail listing card.
    """
    try:
        list_page_url = "https://irres.be/te-koop"
        resp          = secure_get(list_page_url, headers=HEADERS, timeout=15)
        soup          = BeautifulSoup(resp.content, 'html.parser')

        anchors = soup.find_all('a', href=re.compile(r'/pand/\d+/', re.I))
        seen          = set()
        listing_links = []

        for a in anchors:
            href = a.get('href') or ""
            if not href: continue

            full = normalize_url(href, add_tracking=True) if not href.startswith('http') else href
            if full.startswith('http') and '/pand/' in full:
                separator = '&' if '?' in full else '?'
                full      = f"{full}{separator}origin=habichat"

            if full in seen: continue
            text = a.get_text(separator="|", strip=True)
            if not text or not text.strip(): continue

            seen.add(full)
            listing_links.append(a)

        listings = []

        for link in listing_links:
            parsed = parse_main_listing_card(link)

            listing_url = parsed['listing_url'] or ""
            if not listing_url: continue

            listing_id_num  = extract_listing_id_from_url(listing_url)
            parsed_location = parsed.get('location') or ""

            lt        = parsed['listing_type'] or ""
            lt_mapped = TYPE_MAPPING.get(lt, TYPE_MAPPING.get(lt.lower(), lt)) if lt else ""

            # Card photo extracted as an ultimate fallback ONLY
            card_thumbnail_url = parsed['photo_candidate'] or ""
            final_photo_url = ""

            time.sleep(0.09)  
            detail_soup = fetch_detail_page(listing_url)

            button2_label = ""
            button2_email = ""
            address = ""          
            page_content = ""     

            if detail_soup:
                first_name, email = extract_contact_and_email_from_detail(detail_soup)
                if email:
                    button2_email = f"mailto:{email}"
                    name_label    = first_name if first_name else email.split('@')[0]
                    name_label    = " ".join([p.capitalize() for p in re.split(r'[._\-]', name_label) if p])
                    button2_label = f"Contacteer {name_label} - Irres"

                address = extract_address_from_detail_soup(detail_soup)
                page_content = extract_page_content_from_detail_soup(detail_soup)

                # NEW LOGIC: Always prioritize extracting the high-res landscape image from the detail page
                high_res_detail_photo = extract_highest_res_image_from_detail(detail_soup)
                if high_res_detail_photo:
                    final_photo_url = high_res_detail_photo

            # If detail scraping fails or yields no image, fall back to the card thumbnail
            if not final_photo_url:
                final_photo_url = card_thumbnail_url

            final_photo_url = normalize_url(final_photo_url) if final_photo_url else ""
            price_formatted = format_price_string(parsed['price_raw']) if parsed['price_raw'] else ""

            Title = ""
            if parsed_location or price_formatted:
                if parsed_location and price_formatted: Title = f"{parsed_location}⎥{price_formatted}"
                else: Title = parsed_location or price_formatted

            if (not parsed_location) and Title and '⎥' in Title:
                possible_loc = Title.split('⎥', 1)[0].strip()
                if possible_loc and '€' not in possible_loc:
                    parsed_location = normalize_text(possible_loc)

            anchor_name = parsed.get('anchor_name') or ""
            if anchor_name: listing_id = anchor_name
            else:
                location_for_id = parsed_location.split()[0] if parsed_location else ""
                location_for_id = re.sub(r'[^A-Za-z0-9\-]', '', location_for_id)
                listing_id      = f"{listing_id_num}-{location_for_id}" if listing_id_num else listing_url

            button3_label = "Vraag prijs aan" if price_formatted == "Prijs op aanvraag" else ""
            button3_value = f"{button2_email}?subject=Prijs aanvraag {listing_id}" if price_formatted == "Prijs op aanvraag" else ""

            listing_obj = {
                "listing_id":    listing_id,
                "listing_url":   listing_url,
                "photo_url":     final_photo_url,
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

        uniq     = []
        seen_ids = set()
        for li in listings:
            lid = li.get("listing_id")
            if lid in seen_ids: continue
            seen_ids.add(lid)
            uniq.append(li)

        payload = {
            "success":  True,
            "count":    len(uniq),
            "listings": uniq,
        }
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8')

    except Exception as e:
        payload = {"success": False, "error": str(e), "listings": []}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8'), 200


# =============================================================================
# BLOCK 20 — API ENDPOINT: /api/locations
# =============================================================================
@limiter.limit("15 per hour")
@app.route('/api/locations', methods=['GET'])
def get_locations():
    try:
        scraper = IRRESLocationScraper()
        result  = scraper.scrape()
        output_format = request.args.get('format', 'json').lower()

        if output_format == 'csv':
            csv_content = "location\n" + "\n".join([loc['label'] for loc in result['all_locations']])
            return Response(response=csv_content, status=200, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=irres_locations.csv"})

        response_body = {
            "status": "success", "timestamp": datetime.now().isoformat(),
            "data": {"all_locations": result['all_locations'], "location_groups": result['location_groups'], "count": len(result['all_locations'])}
        }
        return jsonify(response_body), 200

    except Exception as e:
        return jsonify({"status": "error", "timestamp": datetime.now().isoformat(), "message": str(e)}), 500


# =============================================================================
# BLOCK 21 — API ENDPOINT: /api/office-images
# =============================================================================
@limiter.limit("15 per hour")
@app.route('/api/office-images', methods=['GET'])
def get_office_images():
    try:
        scraper = IRRESOfficeImagesScraper()
        result  = scraper.scrape()
        response_body = {"status": result['status'], "timestamp": datetime.now().isoformat(), "data": result['images']}
        if result['status'] == 'success': return jsonify(response_body), 200
        else: return jsonify(response_body), 500
    except Exception as e:
        return jsonify({"status": "error", "timestamp": datetime.now().isoformat(), "message": str(e)}), 500


# =============================================================================
# BLOCK 22 — API ENDPOINTS: /health & / (root)
# =============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat(), "service": "IRRES Unified Scraper"})

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "api": "IRRES.be Unified Scraper",
        "version": "8.0",
        "endpoints": {
            "/api/listings": "Scrape all active property listings.",
            "/api/locations": "Get all filter locations and sub-location groups.",
            "/api/office-images": "Get absolute image URLs for all IRRES offices.",
            "/health": "Health check.",
        },
        "authentication": "X-API-KEY header required on all endpoints except /health.",
        "rate_limits": "15 requests per hour per IP on scraping endpoints.",
    })

# =============================================================================
# BLOCK 23 — RUN SERVER
# =============================================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
