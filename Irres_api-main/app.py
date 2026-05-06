# =============================================================================
# BLOCK 1 — IMPORTS
# =============================================================================

import os
import re
import html
import time
import json
import logging
import unicodedata
import asyncio
from datetime import datetime

from flask import Flask, jsonify, Response, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import requests
from bs4 import BeautifulSoup

# NEW
from playwright.async_api import async_playwright


# =============================================================================
# APP INIT
# =============================================================================

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
# SECURITY
# =============================================================================

API_KEY = os.getenv("API_KEY")
if API_KEY is None:
    raise ValueError("API_KEY environment variable is required but not set")


@app.before_request
def require_api_key():
    if request.endpoint == 'static':
        return

    if 'api_key' in request.args:
        return jsonify({"error": "Unauthorized"}), 401

    provided_api_key = request.headers.get('X-API-KEY')

    if not provided_api_key or provided_api_key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


# =============================================================================
# TYPE MAPPING
# =============================================================================

TYPE_MAPPING = {
    'Dwelling': 'Huis',
    'Flat': 'Appartement',
    'Land': 'Grond',
    'dwelling': 'Huis',
    'flat': 'Appartement',
    'land': 'Grond',
}


# =============================================================================
# SECURE GET
# =============================================================================

def secure_get(url, headers=None, timeout=15):
    if not url.startswith("https://"):
        url = "https://irres.be/" + url.lstrip("/")

    response = requests.get(url, headers=headers or HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


# =============================================================================
# PLAYWRIGHT LAZY LOADING
# =============================================================================

async def fetch_full_listings_html():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto("https://irres.be/te-koop", timeout=60000)

        prev_height = 0

        for _ in range(20):
            await page.mouse.wheel(0, 8000)
            await asyncio.sleep(1.2)

            new_height = await page.evaluate("document.body.scrollHeight")

            if new_height == prev_height:
                break
            prev_height = new_height

        html = await page.content()
        await browser.close()

        return html


# =============================================================================
# HELPERS (MINIMAL REQUIRED)
# =============================================================================

def normalize_url(url):
    if not url:
        return ""
    if url.startswith("/"):
        return "https://irres.be" + url
    return url


def extract_listing_id_from_url(url):
    m = re.search(r'/pand/(\d+)', url)
    return m.group(1) if m else ""


def format_price_string(text):
    if not text:
        return ""

    text = text.strip()

    if "Compromis" in text:
        return "Compromis in opmaak"
    if "Prijs op aanvraag" in text:
        return "Prijs op aanvraag"

    digits = re.sub(r"\D", "", text)
    if digits:
        return "€ " + f"{int(digits):,}".replace(",", ".")
    return text


def extract_contact_and_email_from_detail(soup):
    mail = soup.find('a', href=re.compile(r'mailto:'))
    if not mail:
        return "", ""

    email = mail.get('href').replace("mailto:", "")
    name = email.split("@")[0].capitalize()
    return name, email


def extract_address_from_detail_soup(soup):
    p = soup.find_all("p")
    if len(p) >= 2:
        return f"{p[0].text}, {p[1].text}"
    return ""


def extract_page_content_from_detail_soup(soup):
    main = soup.find("main")
    return main.get_text("\n") if main else ""


def fetch_detail_page(url):
    try:
        res = secure_get(url)
        return BeautifulSoup(res.text, "html.parser")
    except:
        return None


# =============================================================================
# CACHE
# =============================================================================

CACHE = {"data": None, "time": 0}
CACHE_TTL = 300


# =============================================================================
# ENDPOINT: /api/listings
# =============================================================================

@limiter.limit("15 per hour")
@app.route('/api/listings', methods=['GET'])
def get_listings():

    now = time.time()
    if CACHE["data"] and now - CACHE["time"] < CACHE_TTL:
        return jsonify(CACHE["data"])

    try:
        html = asyncio.run(fetch_full_listings_html())
        soup = BeautifulSoup(html, "html.parser")

        anchors = soup.select("a[href*='/pand/']")

        listings = []
        seen = set()

        for link in anchors:

            href = link.get("href")
            if not href:
                continue

            url = normalize_url(href)
            if url in seen:
                continue
            seen.add(url)

            listing_id = link.get("name") or ""
            if not listing_id:
                continue

            location = ""
            city = link.select_one(".estate-city")
            if city:
                location = city.get("data-value") or city.text.strip()

            listing_type = ""
            t = link.get("data-estate-type")
            if t:
                listing_type = TYPE_MAPPING.get(t, t)

            description = ""
            desc = link.select_one("p")
            if desc:
                description = desc.text.strip()

            img = link.select_one("img")
            photo_url = normalize_url(img.get("src") if img else "")

            time.sleep(0.1)
            detail = fetch_detail_page(url)

            price = ""
            address = ""
            page_content = ""
            email = ""
            name = ""

            if detail:
                p = detail.find("p")
                price = format_price_string(p.text if p else "")

                address = extract_address_from_detail_soup(detail)
                page_content = extract_page_content_from_detail_soup(detail)
                name, email = extract_contact_and_email_from_detail(detail)

            title = f"{location}⎥{price}"

            listings.append({
                "listing_id": listing_id,
                "listing_url": url,
                "listing_type": listing_type,
                "photo_url": photo_url,
                "title": title,
                "price": price,
                "location": location,
                "description": description,
                "button1_label": "Bekijk het op onze website",
                "button1_value": f"{url}?utm_source=habichat",
                "button2_label": f"Email {name} - Irres" if name else "",
                "button2_value": f"mailto:{email}" if email else "",
                "button3_label": "Vraag prijs aan" if price == "Prijs op aanvraag" else "",
                "button3_value": f"mailto:{email}?subject=Prijs aanvraag {listing_id}" if price == "Prijs op aanvraag" else "",
                "address": address,
                "page_content": page_content
            })

        result = {
            "success": True,
            "count": len(listings),
            "listings": listings
        }

        CACHE["data"] = result
        CACHE["time"] = now

        return jsonify(result)

    except Exception as e:
        return jsonify({"success": False, "error": str(e), "listings": []})


# =============================================================================
# HEALTH
# =============================================================================

@app.route('/health')
def health():
    return jsonify({"status": "ok"})


# =============================================================================
# RUN
# =============================================================================

if __name__ == "__main__":
    app.run(debug=True)
