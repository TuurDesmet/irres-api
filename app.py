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
            
            # Extract photo URL from image tag inside the link
            photo_url = ""
            img_tag = link.find('img')
            if img_tag:
                photo_src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-lazy-src')
                photo_src = normalize_text(photo_src)
                if photo_src:
                    photo_url = photo_src if photo_src.startswith('http') else f"https://irres.be{photo_src}"
            
            # Create listing object
            listing_data = {
                "listing_id": listing_id,
                "listing_url": full_url,
                "photo_url": normalize_text(photo_url),
                "price": parse_price(price) if price else "",
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
