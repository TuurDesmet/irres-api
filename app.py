from flask import Flask, jsonify
from flask import Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import html

app = Flask(__name__)
CORS(app)  # Enable CORS for Botpress calls
# Ensure Flask returns real UTF-8 characters instead of \uXXXX escapes
app.config['JSON_AS_ASCII'] = False

def extract_listing_id(url):
    """Extract listing ID from URL like /pand/8718656/..."""
    match = re.search(r'/pand/(\d+)/', url)
    return match.group(1) if match else None


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

    # Unescape HTML entities like &nbsp;, &euro;, etc.
    s = html.unescape(s)

    # If the string contains literal unicode-escape sequences like "\u00b2" or "\x20",
    # try decoding them. Guard so we don't double-decode already-correct unicode.
    if "\\u" in s or "\\x" in s:
        try:
            s = bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            # If decode fails, leave the string as-is
            pass

    # Normalize whitespace
    s = " ".join(s.split())
    return s


def parse_price(s):
    """Parse the raw scraped price string into one of:
    - integer (euros) if a numeric price is found (rounded to int)
    - 'Prijs op aanvraag' when that phrase appears
    - 'Compromis in opmaak' when that phrase appears
    - empty string when nothing found
    """
    if not s:
        return ""
    s = normalize_text(s)

    # Special exact phrases
    if 'Prijs op aanvraag' in s:
        return 'Prijs op aanvraag'
    if 'Compromis in opmaak' in s or 'Compromis' in s:
        # keep the Dutch phrase the user requested
        return 'Compromis in opmaak'

    # Remove non-digit, non-separator characters but keep euro symbol and common separators
    # Common forms: "€ 1.234.567", "1.234.567 €", "€1.234.567", "1 234 567€"
    # Remove euro sign and whitespace, then strip dots and commas
    cleaned = s.replace('\u20ac', '').replace('€', '')
    cleaned = cleaned.replace('\xa0', ' ').strip()

    # Remove currency words
    cleaned = re.sub(r'(?i)eur[o|s]?|euro', '', cleaned)

    # Keep digits and separators
    cleaned = cleaned.strip()
    # Replace non-digit separators with nothing
    cleaned_digits = re.sub(r'[^0-9]', '', cleaned)

    if not cleaned_digits:
        return ''

    try:
        value = int(cleaned_digits)
        # If the original used cents or weird formatting, it's OK — we treat as euros
        return value
    except Exception:
        return ''


def parse_price_numeric(s):
    """Return numeric price in euros (int) or None if not numeric/special."""
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
    """Return a human-friendly price string with € and thousands separators, or special phrases."""
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
    # Format using dot as thousands separator (e.g. 1.234.567)
    formatted = format(num, ',').replace(',', '.')
    return f'€ {formatted}'


def find_photo_url(link):
    """Try multiple strategies to extract a usable photo URL from a listing link element."""
    # 1. Look for <img> tag and common attributes
    img = link.find('img')
    if img:
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original'):
            val = img.get(attr)
            if val:
                val = normalize_text(val)
                if val:
                    return normalize_url(val)
        # try srcset: pick the largest candidate (last)
        srcset = img.get('srcset')
        if srcset:
            parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
            if parts:
                return normalize_url(normalize_text(parts[-1]))

    # 2. <source> tags inside picture
    source = link.find('source')
    if source:
        for attr in ('srcset', 'data-srcset'):
            val = source.get(attr)
            if val:
                parts = [p.strip().split(' ')[0] for p in val.split(',') if p.strip()]
                if parts:
                    return normalize_url(normalize_text(parts[-1]))

    # 3. background-image in style attribute
    style = link.get('style')
    if style and 'background' in style:
        m = re.search(r'url\(([^)]+)\)', style)
        if m:
            val = m.group(1).strip('"\'')
            return normalize_url(normalize_text(val))

    # 4. Look for data attributes on the link itself
    for attr in ('data-src', 'data-image', 'data-bg'):
        val = link.get(attr)
        if val:
            return normalize_url(normalize_text(val))

    return ''


def normalize_url(src):
    """Normalize URLs to absolute https, handle protocol-relative and root-relative URLs."""
    if not src:
        return ''
    src = src.strip()
    # remove surrounding quotes
    if (src.startswith('"') and src.endswith('"')) or (src.startswith("'") and src.endswith("'")):
        src = src[1:-1]
    # protocol-relative
    if src.startswith('//'):
        return 'https:' + src
    # root-relative
    if src.startswith('/'):
        return 'https://irres.be' + src
    # missing scheme but starts with www or domain
    if src.startswith('www.'):
        return 'https://' + src
    return src

@app.route('/api/listings', methods=['GET'])
def get_listings():
    """Main endpoint to fetch all listings from irres.be/te-koop"""
    try:
        # Fetch main listings page - ALL DATA IS HERE
        url = "https://irres.be/te-koop"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        listings = []
        
        # Find all listing links - these contain the main data
        listing_links = soup.find_all('a', href=re.compile(r'/pand/\d+/'))
        
        for link in listing_links:
            # Extract listing URL and ID
            listing_url = link.get('href', '')
            listing_url = normalize_text(listing_url)
            if not listing_url:
                continue
                
            listing_id = extract_listing_id(listing_url)
            if not listing_id:
                continue
            
            # Full URL
            full_url = f"https://irres.be{listing_url}" if not listing_url.startswith('http') else listing_url
            
            # Extract all text content from the link
            text_content = link.get_text(separator='|', strip=True)
            text_content = normalize_text(text_content)
            parts = [normalize_text(p) for p in text_content.split('|') if p.strip()]
            
            # Initialize variables
            location = ""
            price = ""
            description = ""
            
            # Parse the parts
            # Typically structure is: Location | Location | Price | Description | Type
            for part in parts:
                if '€' in part or 'Prijs op aanvraag' in part:
                    price = part
                elif part in ['Dwelling', 'Flat', 'Huis', 'Appartement', 'Grond']:
                    # Skip property type indicators
                    continue
                elif not location:
                    # First non-price part is usually location
                    location = part
                elif not description and part != location:
                    # Next part is description
                    description = part
            
            # If description is still empty, use the last meaningful part
            if not description and len(parts) > 2:
                description = parts[-2] if parts[-1] in ['Dwelling', 'Flat', 'Huis', 'Appartement', 'Grond'] else parts[-1]
            
            # Extract photo URL using robust helper
            photo_url = find_photo_url(link)
            
            # Create listing object
            listing_data = {
                "listing_id": listing_id,
                "listing_url": full_url,
                "photo_url": normalize_text(photo_url),
                # Provide a formatted price (with €) or special phrases
                "price": format_price(price) if price else "",
                "location": normalize_text(location),
                "description": normalize_text(description)
            }
            
            # Only add if we have at least some data
            if location or price or description:
                listings.append(listing_data)
        
        # Remove duplicates by listing_id (keep first occurrence)
        seen_ids = set()
        unique_listings = []
        for listing in listings:
            if listing['listing_id'] not in seen_ids:
                seen_ids.add(listing['listing_id'])
                unique_listings.append(listing)
        
        # Pretty-print JSON with UTF-8 characters preserved
        import json
        payload = {
            "success": True,
            "count": len(unique_listings),
            "listings": unique_listings
        }
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8')
    
    except Exception as e:
        # Silent fail - return empty list
        import json
        payload = {
            "success": False,
            "error": str(e),
            "listings": []
        }
        # Return 200 so Botpress doesn't break
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8'), 200

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def root():
    """Root endpoint with API info"""
    return jsonify({
        "api": "IRRES.be Listings Scraper",
        "version": "1.0",
        "endpoints": {
            "/api/listings": "Get all property listings",
            "/health": "Health check"
        }
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
