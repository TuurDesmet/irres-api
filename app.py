# app.py
# IRRES.be Listings Scraper API
# UTF-8 encoded. Returns JSON with real UTF-8 characters (m² etc).

from flask import Flask, jsonify, Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import html
import time
import json

app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False  # Ensure UTF-8 characters in JSON

# ==================== CONFIGURATION ====================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

TYPE_MAPPING = {
    'Dwelling': 'Huis',
    'Flat': 'Appartement',
    'Land': 'Grond',
    'dwelling': 'Huis',
    'flat': 'Appartement',
    'land': 'Grond'
}

# ==================== HELPER FUNCTIONS ====================

def normalize_text(s):
    """
    Normalize string: decode unicode escapes, html entities, collapse whitespace.
    Ensures proper UTF-8 handling for characters like m².
    """
    if s is None:
        return ""
    try:
        s = str(s)
    except Exception:
        return ""
    
    # Decode HTML entities (e.g., &nbsp;, &#178; for ²)
    s = html.unescape(s)
    
    # Decode literal unicode escape sequences like "\\u00b2"
    if "\\u" in s or "\\x" in s:
        try:
            s = bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            pass
    
    # Collapse whitespace
    s = " ".join(s.split())
    return s.strip()


def normalize_url(src):
    """
    Make URL absolute for the IRRES.be site.
    Handles protocol-relative, root-relative, and relative paths.
    """
    if not src:
        return ""
    
    src = src.strip().strip('\'"')
    
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return "https://irres.be" + src
    if re.match(r'https?://', src, re.I):
        return src
    if src.startswith("www."):
        return "https://" + src
    
    # Relative path like "uploads_c/..."
    if not re.search(r':', src):
        return "https://irres.be/" + src.lstrip('/')
    
    return src


def extract_listing_id_from_url(url):
    """
    Extract numeric ID from URL pattern: /pand/<id>/...
    Returns empty string if not found.
    """
    if not url:
        return ""
    m = re.search(r'/pand/(\d+)', url)
    return m.group(1) if m else ""


def format_price_string(raw):
    """
    Format price string according to specifications:
    - Returns '€ 1.085.000' for numeric prices (with dot thousands separator)
    - Returns 'Prijs op aanvraag' for price on request
    - Returns 'Compromis in opmaak' for compromis status
    - Returns original normalized string as fallback
    """
    if not raw:
        return ""
    
    s = normalize_text(raw)
    
    # Check for exact phrases
    if re.search(r'Prijs op aanvraag', s, re.I):
        return "Prijs op aanvraag"
    if re.search(r'Compromis', s, re.I):
        return "Compromis in opmaak"
    
    # Extract numeric price
    cleaned = s.replace('€', '').replace('\u20ac', '')
    cleaned = re.sub(r'(?i)prijs op aanvraag|compromis.*', '', cleaned).strip()
    digits = re.sub(r'[^0-9]', '', cleaned)
    
    if not digits:
        return s
    
    try:
        num = int(digits)
        # Format with dot as thousands separator
        formatted = format(num, ',').replace(',', '.')
        return f"€ {formatted}"
    except Exception:
        return s


def parse_main_listing_card(link):
    """
    Parse a listing card from the main /te-koop page.
    Extracts: location, price, description, type, photo, anchor name.
    """
    # Get listing URL
    href = link.get('href') or ""
    href = normalize_text(href)
    if href and not href.startswith('http'):
        href = normalize_url(href)

    # Get site's listing ID from anchor name attribute
    anchor_name = link.get('name') or link.get('data-name') or ""
    anchor_name = normalize_text(anchor_name)

    # Extract location from h2 with class "estate-city"
    location = ""
    city_h2 = link.find('h2', class_=re.compile(r'estate-city'))
    if city_h2:
        # Try data-value attribute first
        location = city_h2.get('data-value', '').strip()
        if not location:
            location = normalize_text(city_h2.get_text())

    # Parse text content
    text = link.get_text(separator="|", strip=True)
    text = normalize_text(text)
    parts = [p for p in [normalize_text(x) for x in text.split("|")] if p]

    price = ""
    description = ""
    listing_type = ""

    # Heuristic parsing for price, type, and description
    for p in parts:
        # Price candidate
        if '€' in p or re.search(r'Prijs op aanvraag', p, re.I) or re.search(r'Compromis', p, re.I):
            price = p
            continue
        
        # Type candidate
        if p in TYPE_MAPPING or p in TYPE_MAPPING.values() or p.lower() in TYPE_MAPPING:
            listing_type = TYPE_MAPPING.get(p, TYPE_MAPPING.get(p.lower(), p))
            continue
        
        # Description (first non-price, non-type, non-location part)
        if not description and p != location and p != listing_type:
            description = p

    # Fallback for description
    if not description and len(parts) >= 2:
        possible = parts[-1]
        if possible not in TYPE_MAPPING and not re.search(r'€', possible) and possible != location:
            description = possible

    # Find photo on the card
    photo_url = find_photo_on_element(link)

    return {
        "listing_url": href,
        "location": location,
        "price_raw": price,
        "description": description,
        "listing_type": listing_type,
        "photo_candidate": photo_url,
        "anchor_name": anchor_name
    }


def find_photo_on_element(el):
    """
    Search for image URL candidates in an element.
    Checks: img src/data-src/srcset, source srcset, style background-image, data attributes.
    Prefers URLs with '/uploads' or common image extensions.
    """
    candidates = []

    # Check img tags
    for img in el.find_all('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'srcset', 'data-srcset'):
            v = img.get(attr)
            if v:
                if 'srcset' in attr:
                    # Parse srcset format (url width, url width, ...)
                    parts = [p.strip().split(' ')[0] for p in v.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(v)

    # Check source tags
    for source in el.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            v = source.get(attr)
            if v:
                if 'srcset' in attr:
                    parts = [p.strip().split(' ')[0] for p in v.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(v)

    # Check inline style background-image
    for node in ([el] + el.find_all(True)):
        style = node.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m:
                candidates.append(m.group(1))

    # Check data attributes
    for attr in ('data-src', 'data-image', 'data-bg', 'data-photo', 'data-thumb', 'data-original'):
        v = el.get(attr)
        if v:
            candidates.append(v)

    # Normalize and filter
    normed = []
    for c in candidates:
        if not c:
            continue
        c = normalize_url(c)
        if c and not c.startswith('data:') and 'svg' not in c.lower():
            normed.append(c)

    # Prefer URLs with uploads paths or image extensions
    for n in normed:
        if re.search(r'/uploads|uploads_c|/siteassets|/panden', n, re.I) or \
           re.search(r'\.(jpg|jpeg|png|webp|gif)(?:\?|$)', n, re.I):
            return n

    # Return first candidate as fallback
    return normed[0] if normed else ""


def find_landscape_image_from_detail(soup):
    """
    On detail page, search for images inside <main data-barba="container">.
    Prefer landscape images when available.
    Returns the best image URL found.
    """
    main = soup.find('main', attrs={'data-barba': True, 'data-barba-namespace': True})
    candidates = []
    search_scope = main if main else soup

    # Find images with src/srcset
    for img in search_scope.find_all('img'):
        for attr in ('srcset', 'data-srcset'):
            srcset = img.get(attr)
            if srcset:
                parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
                candidates.extend(parts)
        for attr in ('src', 'data-src', 'data-original', 'data-lazy-src'):
            v = img.get(attr)
            if v:
                candidates.append(v)

    # Check picture/source tags
    for source in search_scope.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            v = source.get(attr)
            if v:
                if 'srcset' in attr:
                    parts = [p.strip().split(' ')[0] for p in v.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(v)

    # Check style attributes for background images
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
        if not c or c.startswith('data:') or 'svg' in c.lower():
            continue
        normed.append(c)

    # Prefer property uploads paths with image extensions
    for n in normed:
        if re.search(r'/uploads|uploads_c|siteassets|/panden', n, re.I) and \
           re.search(r'\.(jpg|jpeg|png|webp)', n, re.I):
            return n

    # Return first valid candidate
    return normed[0] if normed else ""


def extract_property_details_from_detail_soup(soup):
    """
    Extract property details from the 'Kenmerken' section.
    Looks for <li> elements with data-value attributes containing detail names.
    Returns dict with all detail fields (empty string if not found).
    """
    details = {
        "Terrein_oppervlakte": "",
        "Bewoonbare_oppervlakte": "",
        "Orientatie": "",
        "Slaapkamers": "",
        "Badkamers": "",
        "Bouwjaar": "",
        "Renovatiejaar": "",
        "EPC": "",
        "Beschikbaarheid": ""
    }

    try:
        # Find all <li> elements with data-value attribute
        lis = soup.find_all('li', attrs={'data-value': True})
        
        for li in lis:
            key = li.get('data-value', '').strip()
            # Value is in <p class="pl-6"> or directly in <p>
            p = li.find('p')
            value = normalize_text(p.get_text()) if p else normalize_text(li.get_text())
            
            if not value:
                continue
            
            # Match key to detail field (case-insensitive partial match)
            key_lower = key.lower()
            if 'terrein' in key_lower:
                details["Terrein_oppervlakte"] = value
            elif 'bewoonbare' in key_lower:
                details["Bewoonbare_oppervlakte"] = value
            elif 'ori' in key_lower:
                details["Orientatie"] = value
            elif 'slaap' in key_lower:
                details["Slaapkamers"] = value
            elif 'bad' in key_lower:
                details["Badkamers"] = value
            elif 'bouw' in key_lower and 'reno' not in key_lower:
                details["Bouwjaar"] = value
            elif 'reno' in key_lower:
                details["Renovatiejaar"] = value
            elif 'epc' in key_lower:
                details["EPC"] = value
            elif 'beschik' in key_lower:
                details["Beschikbaarheid"] = value
    except Exception:
        pass

    return details


def extract_contact_and_email_from_detail(soup):
    """
    Extract email and contact first name from detail page.
    Looks for mailto: links in the contact form.
    Returns: (first_name, email)
    """
    email = ""
    first_name = ""

    # Find all mailto links
    all_mailto = soup.find_all('a', href=re.compile(r'^mailto:', re.I))
    for a in all_mailto:
        href = a.get('href', '')
        if not href:
            continue
        m = re.search(r'mailto:([^?]+)', href)
        if m:
            email_candidate = normalize_text(m.group(1))
            if email_candidate:
                email = email_candidate
                break

    # Extract first name from email (everything before @, first token before dot/underscore)
    if email:
        local = email.split('@')[0]
        # Split on dot, underscore, hyphen and take first token
        local_token = re.split(r'[._\-]', local)[0]
        if local_token:
            first_name = local_token.capitalize()

    # Fallback: search for email pattern in page text if no mailto found
    if not email:
        text = soup.get_text(" ", strip=True)
        m2 = re.search(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
        if m2:
            email = normalize_text(m2.group(1))
            local = email.split('@')[0]
            local_token = re.split(r'[._\-]', local)[0] if local else ""
            first_name = local_token.capitalize() if local_token else ""

    return first_name, email


def fetch_detail_page(url, timeout=12):
    """
    Fetch and parse a detail page.
    Returns BeautifulSoup object or None if failed.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return BeautifulSoup(r.content, 'html.parser')
        else:
            return None
    except Exception:
        return None


# ==================== API ENDPOINTS ====================

@app.route('/api/listings', methods=['GET'])
def get_listings():
    """
    Main endpoint: Scrapes all listings from IRRES.be/te-koop
    Returns JSON with complete listing information including contact details and property details.
    """
    try:
        list_page_url = "https://irres.be/te-koop"
        resp = requests.get(list_page_url, headers=HEADERS, timeout=15)
        
        if resp.status_code != 200:
            return Response(
                json.dumps({
                    "success": False,
                    "error": f"Failed to fetch {list_page_url}",
                    "listings": []
                }, ensure_ascii=False, indent=2),
                mimetype='application/json; charset=utf-8'
            ), 200

        soup = BeautifulSoup(resp.content, 'html.parser')

        # Find all listing links (anchors with /pand/<id>/ pattern)
        anchors = soup.find_all('a', href=re.compile(r'/pand/\d+/', re.I))
        
        # Deduplicate and filter for links with text content
        seen = set()
        listing_links = []
        for a in anchors:
            href = a.get('href') or ""
            if not href:
                continue
            
            # Normalize URL
            full = normalize_url(href) if not href.startswith('http') else href
            if full in seen:
                continue
            
            # Only include anchors with text content (listing cards have text)
            text = a.get_text(separator="|", strip=True)
            if not text or not text.strip():
                continue
            
            seen.add(full)
            listing_links.append(a)

        listings = []
        
        # Process each listing
        for link in listing_links:
            parsed = parse_main_listing_card(link)

            listing_url = parsed['listing_url'] or ""
            if not listing_url:
                continue

            listing_id_num = extract_listing_id_from_url(listing_url)
            parsed_location = parsed.get('location') or ""

            # Map listing type to Dutch
            lt = parsed['listing_type'] or ""
            lt_mapped = TYPE_MAPPING.get(lt, TYPE_MAPPING.get(lt.lower(), lt)) if lt else ""

            # Get photo from card (will fetch from detail page if not found)
            photo_url = parsed['photo_candidate'] or ""

            # Fetch detail page for contact info and property details
            time.sleep(0.09)  # Polite delay
            detail_soup = fetch_detail_page(listing_url)
            
            button2_label = ""
            button2_email = ""
            details = {
                "Terrein_oppervlakte": "",
                "Bewoonbare_oppervlakte": "",
                "Orientatie": "",
                "Slaapkamers": "",
                "Badkamers": "",
                "Bouwjaar": "",
                "Renovatiejaar": "",
                "EPC": "",
                "Beschikbaarheid": ""
            }
            
            if detail_soup:
                # Extract contact information
                first_name, email = extract_contact_and_email_from_detail(detail_soup)
                if email:
                    button2_email = f"mailto:{email}"
                    # Format Button2_Label: "Contacteer <Name> - Irres"
                    name_label = first_name if first_name else email.split('@')[0]
                    name_label = " ".join([p.capitalize() for p in re.split(r'[._\-]', name_label) if p])
                    button2_label = f"Contacteer {name_label} - Irres"
                
                # Extract property details
                details_found = extract_property_details_from_detail_soup(detail_soup)
                for k in details.keys():
                    if details_found.get(k):
                        details[k] = details_found[k]

                # Fallback image search if no photo on main page
                if not photo_url:
                    fallback = find_landscape_image_from_detail(detail_soup)
                    if fallback:
                        photo_url = fallback

            # Normalize photo URL
            photo_url = normalize_url(photo_url) if photo_url else ""

            # Format price
            price_formatted = format_price_string(parsed['price_raw']) if parsed['price_raw'] else ""

            # Build Title: "{Location}⎥{Price}"
            Title = ""
            if parsed_location or price_formatted:
                Title = f"{parsed_location}⎥{price_formatted}" if parsed_location and price_formatted else (parsed_location or price_formatted)

            # Update location from Title if needed
            if (not parsed_location) and Title and '⎥' in Title:
                possible_loc = Title.split('⎥', 1)[0].strip()
                if possible_loc and not re.search(r'€', possible_loc):
                    parsed_location = normalize_text(possible_loc)

            # Build listing_id (prefer anchor name from site, fallback to number-location)
            anchor_name = parsed.get('anchor_name') or ""
            if anchor_name:
                listing_id = anchor_name
            else:
                location_for_id = parsed_location.split()[0] if parsed_location else ""
                location_for_id = re.sub(r'[^A-Za-z0-9\-]', '', location_for_id)
                listing_id = f"{listing_id_num}-{location_for_id}" if listing_id_num else listing_url

            # Button1_Label is always the same
            button1 = "Bekijk het op onze website"

            # Button3 logic:
            # - If price is "Prijs op aanvraag", show "Vraag prijs aan" and mailto link
            # - Otherwise, both are empty
            button3_label = "Vraag prijs aan" if price_formatted == "Prijs op aanvraag" else ""
            button3_value = f"{button2_email}?subject=Prijs aanvraag {listing_id}" if price_formatted == "Prijs op aanvraag" else ""

            # Build listing object
            listing_obj = {
                "listing_id": listing_id,
                "listing_url": listing_url,
                "photo_url": photo_url,
                "price": price_formatted,
                "location": parsed_location,
                "description": parsed.get('description') or "",
                "listing_type": lt_mapped,
                "Title": Title,
                "Button1_Label": button1,
                "Button2_Label": button2_label,
                "Button2_email": button2_email,
                "Button3_Label": button3_label,
                "Button3_Value": button3_value,
                "details": {
                    "Terrein_oppervlakte": details.get("Terrein_oppervlakte", ""),
                    "Bewoonbare_oppervlakte": details.get("Bewoonbare_oppervlakte", ""),
                    "Orientatie": details.get("Orientatie", ""),
                    "Slaapkamers": details.get("Slaapkamers", ""),
                    "Badkamers": details.get("Badkamers", ""),
                    "Bouwjaar": details.get("Bouwjaar", ""),
                    "Renovatiejaar": details.get("Renovatiejaar", ""),
                    "EPC": details.get("EPC", ""),
                    "Beschikbaarheid": details.get("Beschikbaarheid", "")
                }
            }

            # Only add listing if it has meaningful content
            if listing_obj.get("location") or listing_obj.get("price") or listing_obj.get("description"):
                listings.append(listing_obj)

        # Deduplicate by listing_id
        uniq = []
        seen_ids = set()
        for li in listings:
            lid = li.get("listing_id")
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            uniq.append(li)

        # Return response
        payload = {
            "success": True,
            "count": len(uniq),
            "listings": uniq
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        )

    except Exception as e:
        payload = {
            "success": False,
            "error": str(e),
            "listings": []
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype='application/json; charset=utf-8'
        ), 200


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy"})


@app.route('/', methods=['GET'])
def root():
    """API information endpoint"""
    return jsonify({
        "api": "IRRES.be Listings Scraper",
        "version": "4.1",
        "endpoints": {
            "/api/listings": "Get all property listings with contact info and property details",
            "/health": "Health check"
        }
    })


# ==================== RUN SERVER ====================

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
