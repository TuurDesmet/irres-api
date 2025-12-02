from flask import Flask, jsonify
from flask import Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import html
import time

app = Flask(__name__)
CORS(app)  # Enable CORS for Botpress calls
# Ensure Flask returns real UTF-8 characters instead of \uXXXX escapes
app.config['JSON_AS_ASCII'] = False

def extract_listing_id(url):
    """Extract listing ID from URL like /pand/8718656/..."""
    match = re.search(r'/pand/(\d+)/', url)
    return match.group(1) if match else None

def map_listing_type(english_type):
    """Map English listing types to Dutch"""
    type_mapping = {
        'Dwelling': 'Huis',
        'Flat': 'Appartement',
        'Land': 'Grond'
    }
    return type_mapping.get(english_type, english_type)

def normalize_text(s):
    """Normalize scraped text:
    - ensure str
    - unescape HTML entities (e.g. &euro;)
    - decode literal backslash unicode escapes (e.g. "\\u00b2") when present
    - collapse extra whitespace
    """
    if s is None:
        return ""
    try:
        s = str(s)
    except Exception:
        return s

    s = html.unescape(s)

    if "\\u" in s or "\\x" in s:
        try:
            s = bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            pass

    s = " ".join(s.split())
    return s


def parse_price(s):
    if not s:
        return ""
    s = normalize_text(s)

    if 'Prijs op aanvraag' in s:
        return 'Prijs op aanvraag'
    if 'Compromis in opmaak' in s or 'Compromis' in s:
        return 'Compromis in opmaak'

    cleaned = s.replace('\u20ac', '').replace('€', '')
    cleaned = cleaned.replace('\xa0', ' ').strip()
    cleaned = re.sub(r'(?i)eur[o|s]?|euro', '', cleaned)
    cleaned = cleaned.strip()
    cleaned_digits = re.sub(r'[^0-9]', '', cleaned)

    if not cleaned_digits:
        return ''

    try:
        return int(cleaned_digits)
    except Exception:
        return ''


def parse_price_numeric(s):
    if not s:
        return None
    s = normalize_text(s)
    if 'Prijs op aanvraag' in s:
        return None
    if 'Compromis in opmaak' in s or 'Compromis' in s:
        return None
    cleaned = s.replace('\u20ac', '').replace('€', '')
    cleaned = cleaned.replace('\xa0', ' ').strip()
    cleaned = re.sub(r'(?i)eur[o|s]?|euro', '', cleaned)
    cleaned_digits = re.sub(r'[^0-9]', '', cleaned)
    if not cleaned_digits:
        return None
    try:
        return int(cleaned_digits)
    except Exception:
        return None


def format_price(s):
    if not s:
        return ''
    s = normalize_text(s)
    if 'Prijs op aanvraag' in s:
        return 'Prijs op aanvraag'
    if 'Compromis in opmaak' in s or 'Compromis' in s:
        return 'Compromis in opmaak'
    num = parse_price_numeric(s)
    if num is None:
        return ''
    formatted = format(num, ',').replace(',', '.')
    return f'€ {formatted}'


###############################################
# FIXED FUNCTION: NO MORE PARENT SCANNING
###############################################
def find_photo_url(link):
    """Extract photo ONLY from inside the listing link (never parent)."""
    def norm_candidate(val):
        if not val:
            return None
        v = normalize_text(val)
        if not v:
            return None
        if v.lower().startswith('data:image'):
            return None
        return normalize_url(v)

    candidates = []

    # Images inside the <a>
    for img in link.select('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'data-srcset'):
            raw = img.get(attr)
            if raw:
                candidates.append(raw)
        srcset = img.get('srcset') or img.get('data-srcset')
        if srcset:
            parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
            candidates.extend(parts)

    # <source> tags inside <a>
    for source in link.select('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            raw = source.get(attr)
            if raw:
                if 'srcset' in attr:
                    parts = [p.strip().split(' ')[0] for p in raw.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(raw)

    # style="background-image:url()"
    def append_style(el):
        style = el.get('style')
        if style and 'url(' in style:
            m = re.search(r'url\(([^)]+)\)', style)
            if m:
                candidates.append(m.group(1).strip('"\''))

    append_style(link)
    for desc in link.descendants:
        if hasattr(desc, 'get'):
            append_style(desc)

    # data-* attributes only inside <a>
    for desc in link.descendants:
        if hasattr(desc, 'get'):
            for attr in ('data-src', 'data-image', 'data-bg', 'data-photo', 'data-thumb'):
                raw = desc.get(attr)
                if raw:
                    candidates.append(raw)

    # Normalize
    normed = []
    for c in candidates:
        n = norm_candidate(c)
        if n:
            normed.append(n)

    for n in normed:
        if re.search(r'/uploads|uploads_c|/uploads_c/', n, re.I) or re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', n, re.I):
            return n

    return normed[0] if normed else ''


###############################################
# Fallback stays UNCHANGED
###############################################
def find_fallback_image(soup):
    candidates = []

    all_imgs = soup.find_all('img')
    for img in all_imgs:
        srcset = img.get('srcset') or img.get('data-srcset')
        if srcset:
            parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
            for part in parts:
                if part and not part.startswith('data:'):
                    candidates.append(part)

    for img in all_imgs:
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'data-srcset'):
            img_url = img.get(attr)
            if img_url and not img_url.startswith('data:'):
                candidates.append(img_url)

    for source in soup.find_all('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            img_url = source.get(attr)
            if img_url:
                if attr in ('srcset', 'data-srcset'):
                    parts = [p.strip().split(' ')[0] for p in img_url.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(img_url)

    for el in soup.find_all(style=True):
        style = el.get('style', '')
        if 'url(' in style:
            match = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
            if match:
                candidates.append(match.group(1))

    valid_images = []
    for url in candidates:
        if not url or 'svg' in url.lower() or url.startswith('data:'):
            continue
        normalized = normalize_url(url)
        if not normalized:
            continue
        if any(pattern in normalized.lower() for pattern in ['team/', 'logo', 'icon', 'avatar']):
            continue
        valid_images.append(normalized)

    for img_url in valid_images:
        if re.search(r'/uploads|uploads_c|/uploads_c/|/panden/|siteassets', img_url, re.I):
            if re.search(r'\.(jpg|jpeg|png|webp)', img_url, re.I):
                return img_url

    return valid_images[0] if valid_images else ''


def normalize_url(src):
    if not src:
        return ''
    src = src.strip()
    if (src.startswith('"') and src.endswith('"')) or (src.startswith("'") and src.endswith("'")):
        src = src[1:-1]
    if src.startswith('//'):
        return 'https:' + src
    if src.startswith('/'):
        return 'https://irres.be' + src
    if src.startswith('www.'):
        return 'https://' + src
    if re.match(r'https?://', src, re.I):
        return src
    if src.startswith('./'):
        src = src[2:]
    if not re.search(r':', src):
        return 'https://irres.be/' + src.lstrip('/')
    return src


###############################################
# (THE REST OF YOUR CODE BELOW IS UNCHANGED)
###############################################

def extract_property_details(soup):
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
        list_items = soup.find_all('li', {'data-value': True})
        
        for item in list_items:
            data_value = item.get('data-value', '')
            p_tag = item.find('p')
            if p_tag:
                value = normalize_text(p_tag.get_text())
                
                if data_value == "Terrein oppervlakte":
                    details["Terrein_oppervlakte"] = value
                elif data_value == "Bewoonbare oppervlakte":
                    details["Bewoonbare_oppervlakte"] = value
                elif data_value == "Oriëntatie":
                    details["Orientatie"] = value
                elif data_value == "Slaapkamers":
                    details["Slaapkamers"] = value
                elif data_value == "Badkamers":
                    details["Badkamers"] = value
                elif data_value == "Bouwjaar":
                    details["Bouwjaar"] = value
                elif data_value == "Renovatiejaar":
                    details["Renovatiejaar"] = value
                elif data_value == "EPC":
                    details["EPC"] = value
                elif data_value == "Beschikbaarheid":
                    details["Beschikbaarheid"] = value
    
    except Exception as e:
        print(f"Error extracting property details: {str(e)}")
    
    return details


def extract_contact_and_details(listing_url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(listing_url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        first_name = ""
        email_address = ""
        fallback_image = ""
        
        footer_form = soup.find('form', class_='estate-footer')
        
        if footer_form:
            paragraphs = footer_form.find_all('p')
            
            for p in paragraphs:
                email_link = p.find('a', href=re.compile(r'^mailto:'))
                if email_link:
                    email_href = email_link.get('href', '')
                    email_match = re.search(r'mailto:([^\s]+)', email_href)
                    if email_match:
                        email_address = normalize_text(email_match.group(1))
                        break
            
            name_p = footer_form.find('p', class_='font-bold')
            if name_p:
                full_name = normalize_text(name_p.get_text())
                if full_name:
                    first_name = full_name.split()[0] if full_name.split() else full_name
        
        if not email_address:
            all_email_links = soup.find_all('a', href=re.compile(r'^mailto:'))
            for link in all_email_links:
                email_href = link.get('href', '')
                email_match = re.search(r'mailto:([^\s]+)', email_href)
                if email_match:
                    email_address = normalize_text(email_match.group(1))
                    break
        
        fallback_image = find_fallback_image(soup)
        
        property_details = extract_property_details(soup)
        
        return first_name, email_address, fallback_image, property_details
        
    except Exception as e:
        print(f"Error fetching info for {listing_url}: {str(e)}")
        return "", "", "", {}


@app.route('/api/listings', methods=['GET'])
def get_listings():
    try:
        url = "https://irres.be/te-koop"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        listings = []
        
        listing_links = soup.find_all('a', href=re.compile(r'/pand/\d+/'))
        
        for link in listing_links:
            listing_url = link.get('href', '')
            listing_url = normalize_text(listing_url)
            if not listing_url:
                continue
                
            listing_id = extract_listing_id(listing_url)
            if not listing_id:
                continue
            
            full_url = f"https://irres.be{listing_url}" if not listing_url.startswith('http') else listing_url
            
            text_content = link.get_text(separator='|', strip=True)
            text_content = normalize_text(text_content)
            parts = [normalize_text(p) for p in text_content.split('|') if p.strip()]
            
            location = ""
            price = ""
            description = ""
            listing_type = ""
            
            for part in parts:
                if '€' in part or 'Prijs op aanvraag' in part or 'Compromis' in part:
                    price = part
                    continue
                if part in ['Dwelling', 'Flat', 'Huis', 'Appartement', 'Grond', 'Land']:
                    listing_type = map_listing_type(part)
                    continue
                if not location:
                    location = part
                    continue
                if not description and part != location:
                    description = part
            
            if not description and len(parts) > 2:
                if parts[-1] in ['Dwelling', 'Flat', 'Huis', 'Appartement', 'Grond', 'Land']:
                    description = parts[-2] if len(parts) > 1 else ""
                else:
                    description = parts[-1]
            
            photo_url = find_photo_url(link)
            
            time.sleep(0.1)
            first_name, email_address, fallback_image, property_details = extract_contact_and_details(full_url)
            
            if not photo_url or not photo_url.strip():
                if fallback_image:
                    photo_url = fallback_image
                    print(f"Using fallback image for listing {listing_id}: {fallback_image}")
                else:
                    print(f"No image found for listing {listing_id}")
            else:
                print(f"Using main page image for listing {listing_id}: {photo_url}")
            
            formatted_price = format_price(price) if price else ""
            
            title = f"{location}⎢{formatted_price}" if location or formatted_price else ""
            
            listing_data = {
                "listing_id": listing_id,
                "listing_url": full_url,
                "photo_url": normalize_text(photo_url),
                "price": formatted_price,
                "location": normalize_text(location),
                "description": normalize_text(description),
                "listing_type": normalize_text(listing_type),
                "Title": normalize_text(title),
                "Button1_Label": "Bekijk het op onze website",
                "Button2_Label": f"Contacteer {first_name} - Irres" if first_name else "Contacteer Irres",
                "Button2_email": f"mailto:{email_address}" if email_address else "",
                "details": {
                    "Terrein_oppervlakte": property_details.get("Terrein_oppervlakte", ""),
                    "Bewoonbare_oppervlakte": property_details.get("Bewoonbare_oppervlakte", ""),
                    "Orientatie": property_details.get("Orientatie", ""),
                    "Slaapkamers": property_details.get("Slaapkamers", ""),
                    "Badkamers": property_details.get("Badkamers", ""),
                    "Bouwjaar": property_details.get("Bouwjaar", ""),
                    "Renovatiejaar": property_details.get("Renovatiejaar", ""),
                    "EPC": property_details.get("EPC", ""),
                    "Beschikbaarheid": property_details.get("Beschikbaarheid", "")
                }
            }
            
            if location or price or description:
                listings.append(listing_data)
        
        seen_ids = set()
        unique_listings = []
        for listing in listings:
            if listing['listing_id'] not in seen_ids:
                seen_ids.add(listing['listing_id'])
                unique_listings.append(listing)
        
        import json
        payload = {
            "success": True,
            "count": len(unique_listings),
            "listings": unique_listings
        }
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8')
    
    except Exception as e:
        import json
        payload = {
            "success": False,
            "error": str(e),
            "listings": []
        }
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8'), 200

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "api": "IRRES.be Listings Scraper",
        "version": "3.0",
        "endpoints": {
            "/api/listings": "Get all property listings with contact info and property details",
            "/health": "Health check"
        }
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
