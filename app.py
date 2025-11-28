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
    def norm_candidate(val):
        if not val:
            return None
        v = normalize_text(val)
        if not v:
            return None
        # skip data URIs and tiny svg placeholders
        if v.lower().startswith('data:image'):
            return None
        return normalize_url(v)

    candidates = []

    # gather from img tags (all descendants)
    for img in link.select('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'data-srcset'):
            raw = img.get(attr)
            if raw:
                candidates.append(raw)
        # srcset entries
        srcset = img.get('srcset') or img.get('data-srcset')
        if srcset:
            parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
            candidates.extend(parts)

    # gather from <source> tags
    for source in link.select('source'):
        for attr in ('srcset', 'data-srcset', 'src'):
            raw = source.get(attr)
            if raw:
                if attr in ('srcset', 'data-srcset'):
                    parts = [p.strip().split(' ')[0] for p in raw.split(',') if p.strip()]
                    candidates.extend(parts)
                else:
                    candidates.append(raw)

    # gather from style attributes in link and descendants
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

    # gather from data attributes on link and descendants
    for attr in ('data-src', 'data-image', 'data-bg', 'data-photo', 'data-thumb'):
        raw = link.get(attr)
        if raw:
            candidates.append(raw)
    for desc in link.descendants:
        if hasattr(desc, 'get'):
            for attr in ('data-src', 'data-image', 'data-bg', 'data-photo', 'data-thumb'):
                raw = desc.get(attr)
                if raw:
                    candidates.append(raw)

    # broaden: look in parent container
    parent = link.parent
    if parent:
        for img in parent.select('img, source'):
            for attr in ('src', 'data-src', 'data-lazy-src', 'data-original'):
                raw = img.get(attr)
                if raw:
                    candidates.append(raw)
            srcset = img.get('srcset')
            if srcset:
                parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
                candidates.extend(parts)

    # Normalize candidates and filter
    normed = []
    for c in candidates:
        n = norm_candidate(c)
        if n:
            normed.append(n)

    # prefer URLs pointing to uploads or with common image extensions
    for n in normed:
        if re.search(r'/uploads|uploads_c|/uploads_c/', n, re.I) or re.search(r'\.(jpg|jpeg|png|webp|gif)(?:\?|$)', n, re.I):
            return n

    # otherwise return first valid normalized candidate
    if normed:
        return normed[0]

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
    # missing scheme but starts with www
    if src.startswith('www.'):
        return 'https://' + src
    # already absolute
    if re.match(r'https?://', src, re.I):
        return src
    # remove leading ./
    if src.startswith('./'):
        src = src[2:]
    # If it's a relative path like 'uploads_c/...' make it absolute to the site
    if not re.search(r':', src):
        return 'https://irres.be/' + src.lstrip('/')
    return src


def extract_contact_info(listing_url):
    """
    Fetch the listing detail page and extract contact person's first name and email.
    Returns tuple: (first_name, email_address)
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(listing_url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        first_name = ""
        email_address = ""
        
        # Look for the contact form section with class "estate-footer"
        footer_form = soup.find('form', class_='estate-footer')
        
        if footer_form:
            # Find all paragraph tags that might contain name and email
            paragraphs = footer_form.find_all('p')
            
            for p in paragraphs:
                # Look for email links
                email_link = p.find('a', href=re.compile(r'^mailto:'))
                if email_link:
                    email_href = email_link.get('href', '')
                    # Extract email from mailto: link
                    email_match = re.search(r'mailto:([^\s]+)', email_href)
                    if email_match:
                        email_address = normalize_text(email_match.group(1))
                        break
            
            # Look for the name - it's usually in a <p> tag with font-bold class
            name_p = footer_form.find('p', class_='font-bold')
            if name_p:
                full_name = normalize_text(name_p.get_text())
                # Extract first name (everything before the first space)
                if full_name:
                    first_name = full_name.split()[0] if full_name.split() else full_name
        
        # Fallback: try to find email anywhere on the page if not found in form
        if not email_address:
            all_email_links = soup.find_all('a', href=re.compile(r'^mailto:'))
            for link in all_email_links:
                email_href = link.get('href', '')
                email_match = re.search(r'mailto:([^\s]+)', email_href)
                if email_match:
                    email_address = normalize_text(email_match.group(1))
                    break
        
        return first_name, email_address
        
    except Exception as e:
        # If we can't fetch the detail page, return empty strings
        return "", ""


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
            listing_type = ""
            
            # Parse the parts
            # Typically structure is: Location | Location | Price | Description | Type
            for part in parts:
                # Treat explicit price indicators and placeholders as price
                if '€' in part or 'Prijs op aanvraag' in part or 'Compromis' in part:
                    price = part
                    continue
                if part in ['Dwelling', 'Flat', 'Huis', 'Appartement', 'Grond', 'Land']:
                    # Property type - map to Dutch
                    listing_type = map_listing_type(part)
                    continue
                if not location:
                    # First non-price part is usually location
                    location = part
                    continue
                # Next non-price non-location part is description
                if not description and part != location:
                    description = part
            
            # If description is still empty, use the last meaningful part
            if not description and len(parts) > 2:
                # Check if last part is a type, if so use second to last
                if parts[-1] in ['Dwelling', 'Flat', 'Huis', 'Appartement', 'Grond', 'Land']:
                    description = parts[-2] if len(parts) > 1 else ""
                else:
                    description = parts[-1]
            
            # Extract photo URL using robust helper
            photo_url = find_photo_url(link)
            
            # Extract contact information from the listing detail page
            first_name, email_address = extract_contact_info(full_url)
            
            # Format the price for display
            formatted_price = format_price(price) if price else ""
            
            # Create the title in the format: "{location}⎢{price}"
            title = f"{location}⎢{formatted_price}" if location or formatted_price else ""
            
            # Create listing object with new fields
            listing_data = {
                "listing_id": listing_id,
                "listing_url": full_url,
                "photo_url": normalize_text(photo_url),
                "price": formatted_price,
                "location": normalize_text(location),
                "description": normalize_text(description),
                "listing_type": normalize_text(listing_type),
                # New fields
                "Title": normalize_text(title),
                "Button1_Label": "Bekijk het op onze website",
                "Button2_Label": f"Contacteer {first_name} - Irres" if first_name else "Contacteer Irres",
                "Button2_email": f"mailto:{email_address}" if email_address else ""
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
        "version": "2.0",
        "endpoints": {
            "/api/listings": "Get all property listings with contact info",
            "/health": "Health check"
        }
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)