# app.py
# IRRES.be Listings Scraper - Fixed to ensure all fields are always strings
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
app.config['JSON_AS_ASCII'] = False  # ensure UTF-8 characters in JSON

# ------------- Helpers ----------------

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


def normalize_text(s):
    """Normalize string: unicode escapes, html entities, collapse whitespace."""
    if s is None:
        return ""
    try:
        s = str(s)
    except Exception:
        return ""
    s = html.unescape(s)
    # decode literal unicode escape sequences like "\\u00b2"
    if "\\u" in s or "\\x" in s:
        try:
            s = bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            pass
    # collapse whitespace
    s = " ".join(s.split())
    return s.strip()


def normalize_url(src):
    """Make URL absolute for the site, handle protocol-relative, root-relative."""
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
    # if relative path like "uploads_c/..."
    if not re.search(r':', src):
        return "https://irres.be/" + src.lstrip('/')
    return src


def extract_listing_id_from_url(url):
    """Return numeric id found in /pand/<id>/..."""
    if not url:
        return ""
    m = re.search(r'/pand/(\d+)', url)
    return m.group(1) if m else ""


def format_price_string(raw):
    """Return formatted price: '€ 1.085.000' or 'Prijs op aanvraag' or 'Compromis in opmaak' or ''."""
    if not raw:
        return ""
    s = normalize_text(raw)
    # exact phrases
    if re.search(r'Prijs op aanvraag', s, re.I):
        return "Prijs op aanvraag"
    if re.search(r'Compromis', s, re.I):
        return "Compromis in opmaak"
    # look for euro amounts
    # remove euro symbol and extract digits
    cleaned = s.replace('€', '').replace('\u20ac', '')
    cleaned = re.sub(r'(?i)prijs op aanvraag|compromis.*', '', cleaned).strip()
    digits = re.sub(r'[^0-9]', '', cleaned)
    if not digits:
        return s  # fallback return original normalized
    try:
        num = int(digits)
        # format with dot thousands
        formatted = format(num, ',').replace(',', '.')
        return f"€ {formatted}"
    except Exception:
        return s


def parse_main_listing_card(link):
    """
    Given an <a> tag from the main /te-koop page that links to a listing,
    attempt to extract location, price, description, type, photo candidate.
    """
    # listing_url
    href = link.get('href') or ""
    href = normalize_text(href)
    if href and not href.startswith('http'):
        href = normalize_url(href)

    # extract anchor 'name' attribute if present
    anchor_name = link.get('name') or link.get('data-name') or ""
    anchor_name = normalize_text(anchor_name)

    # Extract location from h2 with class "estate-city" and data-value attribute
    location = ""
    city_h2 = link.find('h2', class_=re.compile(r'estate-city'))
    if city_h2:
        location = city_h2.get('data-value', '').strip()
        if not location:
            location = normalize_text(city_h2.get_text())

    # text parts inside the link
    text = link.get_text(separator="|", strip=True)
    text = normalize_text(text)
    parts = [p for p in [normalize_text(x) for x in text.split("|")] if p]

    price = ""
    description = ""
    listing_type = ""

    # heuristic parsing for price, type, description:
    for p in parts:
        # price candidate
        if '€' in p or re.search(r'Prijs op aanvraag', p, re.I) or re.search(r'Compromis', p, re.I):
            price = p
            continue
        # type candidate
        if p in TYPE_MAPPING or p in TYPE_MAPPING.values() or p.lower() in TYPE_MAPPING:
            listing_type = TYPE_MAPPING.get(p, TYPE_MAPPING.get(p.lower(), p))
            continue
        # first non-price non-type and not location => description if description empty
        if not description and p != location and p != listing_type:
            description = p

    # final fallback for description
    if not description and len(parts) >= 2:
        possible = parts[-1]
        if possible not in TYPE_MAPPING and not re.search(r'€', possible) and possible != location:
            description = possible

    # try to find photo on the card
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
    """Search for image url candidates in an element."""
    candidates = []

    # images
    for img in el.find_all('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'data-srcset'):
            v = img.get(attr)
            if v:
                if attr in ('data-srcset', 'srcset'):
                    parts = [p.strip().split(' ')[0] for p in v.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(v)
    # source tags
    for source in el.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            v = source.get(attr)
            if v:
                if attr in ('srcset', 'data-srcset'):
                    parts = [p.strip().split(' ')[0] for p in v.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(v)

    # inline style background-image
    for node in ([el] + el.find_all(True)):
        style = node.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m:
                candidates.append(m.group(1))

    # data attributes
    for attr in ('data-src', 'data-image', 'data-bg', 'data-photo', 'data-thumb', 'data-original'):
        v = el.get(attr)
        if v:
            candidates.append(v)
    
    # normalize and choose best
    normed = []
    for c in candidates:
        if not c:
            continue
        c = normalize_url(c)
        if c and not c.startswith('data:') and 'svg' not in c.lower():
            normed.append(c)
    
    # prefer uploads paths or explicit image extensions
    for n in normed:
        if re.search(r'/uploads|uploads_c|/siteassets|/panden|/uploads_c/', n, re.I) or re.search(r'\.(jpg|jpeg|png|webp|gif)(?:\?|$)', n, re.I):
            return n
    # fallback first candidate
    return normed[0] if normed else ""


def find_landscape_image_from_detail(soup):
    """On the detail page, search for images."""
    main = soup.find('main', attrs={'data-barba': True, 'data-barba-namespace': True})
    candidates = []
    search_scope = main if main else soup

    # find images with src/srcset
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
    
    # picture/source tags
    for source in search_scope.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            v = source.get(attr)
            if v:
                if attr in ('srcset', 'data-srcset'):
                    parts = [p.strip().split(' ')[0] for p in v.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(v)
    
    # style attributes
    for el in search_scope.find_all(style=True):
        style = el.get('style') or ""
        if 'url(' in style:
            m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if m:
                candidates.append(m.group(1))

    # normalize & filter
    normed = []
    for c in candidates:
        if not c:
            continue
        c = normalize_url(c)
        if not c or c.startswith('data:') or 'svg' in c.lower():
            continue
        normed.append(c)

    # prefer property uploads paths
    for n in normed:
        if re.search(r'/uploads|uploads_c|siteassets|/panden', n, re.I) and re.search(r'\.(jpg|jpeg|png|webp)', n, re.I):
            return n

    return normed[0] if normed else ""


def extract_property_details_from_detail_soup(soup):
    """Extract details from the 'Kenmerken' list structure."""
    out = {
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
        lis = soup.find_all('li', attrs={'data-value': True})
        for li in lis:
            key = li.get('data-value', '').strip()
            p = li.find('p')
            value = normalize_text(p.get_text()) if p else normalize_text(li.get_text())
            if not value:
                continue
            if key.lower().startswith('terrein'):
                out["Terrein_oppervlakte"] = value
            elif key.lower().startswith('bewoonbare'):
                out["Bewoonbare_oppervlakte"] = value
            elif key.lower().startswith('ori'):
                out["Orientatie"] = value
            elif key.lower().startswith('slaap'):
                out["Slaapkamers"] = value
            elif key.lower().startswith('bad'):
                out["Badkamers"] = value
            elif key.lower().startswith('bouw'):
                out["Bouwjaar"] = value
            elif key.lower().startswith('reno'):
                out["Renovatiejaar"] = value
            elif key.lower().startswith('epc'):
                out["EPC"] = value
            elif key.lower().startswith('beschik'):
                out["Beschikbaarheid"] = value
    except Exception:
        pass

    return out


def extract_contact_and_email_from_detail(soup):
    """Extract email and contact first name from the detail page."""
    email = ""
    first_name = ""

    # find mailto link
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

    # first name from email
    if email:
        local = email.split('@')[0]
        local_token = re.split(r'[._\-]', local)[0]
        if local_token:
            first_name = local_token.capitalize()

    # fallback: find email in page text
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
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return BeautifulSoup(r.content, 'html.parser')
        else:
            return None
    except Exception:
        return None


@app.route('/api/listings', methods=['GET'])
def get_listings():
    try:
        list_page_url = "https://irres.be/te-koop"
        resp = requests.get(list_page_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return Response(
                json.dumps({"success": False, "error": f"Failed to fetch {list_page_url}", "listings": []}, 
                          ensure_ascii=False, indent=2), 
                mimetype='application/json; charset=utf-8'
            ), 200

        soup = BeautifulSoup(resp.content, 'html.parser')

        # find all anchors linking to /pand/<id>
        anchors = soup.find_all('a', href=re.compile(r'/pand/\d+/', re.I))
        seen = set()
        listing_links = []
        
        for a in anchors:
            href = a.get('href') or ""
            if not href:
                continue
            full = normalize_url(href) if not href.startswith('http') else href
            if full in seen:
                continue
            text = a.get_text(separator="|", strip=True)
            if not text or not text.strip():
                continue
            seen.add(full)
            listing_links.append(a)

        listings = []
        for link in listing_links:
            parsed = parse_main_listing_card(link)

            listing_url = parsed['listing_url'] or ""
            if not listing_url:
                continue

            listing_id_num = extract_listing_id_from_url(listing_url)
            parsed_location = parsed.get('location', '') or ""

            # listing_type mapping
            lt = parsed.get('listing_type', '') or ""
            lt_mapped = TYPE_MAPPING.get(lt, TYPE_MAPPING.get(lt.lower(), lt)) if lt else ""

            # photo selection
            photo_url = parsed.get('photo_candidate', '') or ""

            # fetch detail page
            time.sleep(0.09)
            detail_soup = fetch_detail_page(listing_url)
            
            # Initialize with empty strings
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
                # contact
                first_name, email = extract_contact_and_email_from_detail(detail_soup)
                if email:
                    button2_email = f"mailto:{email}"
                    name_label = first_name if first_name else email.split('@')[0]
                    name_label = " ".join([p.capitalize() for p in re.split(r'[._\-]', name_label) if p])
                    button2_label = f"Contacteer {name_label} - Irres"
                
                # property details
                details_found = extract_property_details_from_detail_soup(detail_soup)
                for k in details.keys():
                    if details_found.get(k):
                        details[k] = details_found[k]

                # fallback image
                if not photo_url:
                    fallback = find_landscape_image_from_detail(detail_soup)
                    if fallback:
                        photo_url = fallback

            # final normalization
            photo_url = normalize_url(photo_url) if photo_url else ""
            price_formatted = format_price_string(parsed.get('price_raw', '')) if parsed.get('price_raw') else ""

            # Title
            Title = ""
            if parsed_location or price_formatted:
                Title = f"{parsed_location}⎥{price_formatted}" if parsed_location and price_formatted else (parsed_location or price_formatted)

            # Extract location from Title if needed
            if (not parsed_location) and Title and '⎥' in Title:
                possible_loc = Title.split('⎥', 1)[0].strip()
                if possible_loc and not re.search(r'€', possible_loc):
                    parsed_location = normalize_text(possible_loc)

            # listing_id
            anchor_name = parsed.get('anchor_name', '') or ""
            if anchor_name:
                listing_id = anchor_name
            else:
                location_for_id = parsed_location.split()[0] if parsed_location else ""
                location_for_id = re.sub(r'[^A-Za-z0-9\-]', '', location_for_id)
                listing_id = f"{listing_id_num}-{location_for_id}" if listing_id_num else listing_url

            # Button labels
            button1 = "Bekijk het op onze website"
            button3_label = "Vraag prijs aan" if price_formatted == "Prijs op aanvraag" else ""
            button3_value = f"{button2_email}?subject=Prijs aanvraag {listing_id}" if price_formatted == "Prijs op aanvraag" else ""

            # Convert details dict to JSON string for storage
            details_json = json.dumps(details, ensure_ascii=False)

            # Build the object - ENSURE ALL FIELDS ARE STRINGS
            listing_obj = {
                "listing_id": str(listing_id),
                "listing_url": str(listing_url),
                "photo_url": str(photo_url),
                "price": str(price_formatted),
                "location": str(parsed_location),
                "description": str(parsed.get('description', '') or ""),
                "listing_type": str(lt_mapped),
                "Title": str(Title),
                "Button1_Label": str(button1),
                "Button2_Label": str(button2_label),
                "Button2_email": str(button2_email),
                "Button3_Label": str(button3_label),
                "Button3_Value": str(button3_value),
                "details": details_json  # Store as JSON string
            }

            # Add listing only if it has some content
            if listing_obj.get("location") or listing_obj.get("price") or listing_obj.get("description"):
                listings.append(listing_obj)

        # deduplicate by listing_id
        uniq = []
        seen_ids = set()
        for li in listings:
            lid = li.get("listing_id")
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            uniq.append(li)

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
        payload = {"success": False, "error": str(e), "listings": []}
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2), 
            mimetype='application/json; charset=utf-8'
        ), 200


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})


@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "api": "IRRES.be Listings Scraper",
        "version": "4.1",
        "endpoints": {
            "/api/listings": "Get all property listings with contact info and property details",
            "/health": "Health check"
        }
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
