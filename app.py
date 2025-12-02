from flask import Flask, jsonify, Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import html
import time

app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False

def normalize_text(s):
    if not s:
        return ""
    s = html.unescape(str(s))
    s = " ".join(s.split())
    return s

def normalize_url(src):
    if not src:
        return ''
    src = src.strip().strip('"').strip("'")
    if src.startswith('//'):
        return 'https:' + src
    if src.startswith('/'):
        return 'https://irres.be' + src
    if src.startswith('www.'):
        return 'https://' + src
    if re.match(r'https?://', src):
        return src
    return 'https://irres.be/' + src.lstrip('/')

def extract_listing_id(url):
    match = re.search(r'/pand/(\d+)/', url)
    return match.group(1) if match else None

def map_listing_type(english_type):
    mapping = {'Dwelling': 'Huis', 'Flat': 'Appartement', 'Land': 'Grond'}
    return mapping.get(english_type, english_type)

def find_photo_url(link):
    """Extract photo from main page link"""
    for img in link.select('img'):
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'data-srcset'):
            val = img.get(attr)
            if val and not val.startswith('data:'):
                return normalize_url(val)
    return None

def find_fallback_image(soup):
    """Find a horizontal/landscape image from listing detail page"""
    candidates = []
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
        if src and not src.startswith('data:'):
            candidates.append(normalize_url(src))
    return candidates[0] if candidates else None

def extract_contact_and_details(listing_url):
    """Fetch listing page and extract first name, email, fallback image"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(listing_url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.content, 'html.parser')
        first_name = ""
        email = ""
        fallback_image = find_fallback_image(soup)
        
        footer = soup.find('form', class_='estate-footer')
        if footer:
            name_p = footer.find('p', class_='font-bold')
            if name_p:
                first_name = normalize_text(name_p.get_text()).split()[0]
            email_link = footer.find('a', href=re.compile(r'^mailto:'))
            if email_link:
                email_match = re.search(r'mailto:([^\s]+)', email_link.get('href',''))
                if email_match:
                    email = normalize_text(email_match.group(1))
        return first_name, email, fallback_image
    except Exception as e:
        print(f"Error fetching details for {listing_url}: {e}")
        return "", "", None

@app.route('/api/listings', methods=['GET'])
def get_listings():
    try:
        url = "https://irres.be/te-koop"
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.content, 'html.parser')
        
        listings = []
        listing_links = soup.find_all('a', href=re.compile(r'/pand/\d+/'))
        
        for link in listing_links:
            listing_url = normalize_text(link.get('href'))
            if not listing_url:
                continue
            listing_id = extract_listing_id(listing_url)
            if not listing_id:
                continue
            full_url = listing_url if listing_url.startswith('http') else 'https://irres.be' + listing_url
            
            photo_url = find_photo_url(link)
            if not photo_url:
                # fetch fallback image from listing page
                time.sleep(0.1)
                _, _, fallback_image = extract_contact_and_details(full_url)
                photo_url = fallback_image or None
            
            # Extract basic text
            text = normalize_text(link.get_text(separator='|', strip=True))
            parts = [p for p in text.split('|') if p.strip()]
            location = parts[0] if parts else ""
            price = next((p for p in parts if '€' in p or 'Prijs op aanvraag' in p), "")
            description = next((p for p in parts if p not in [location, price]), "")
            listing_type = next((map_listing_type(p) for p in parts if p in ['Dwelling','Flat','Land','Huis','Appartement','Grond']), "")
            
            # Contact info
            first_name, email, _ = extract_contact_and_details(full_url)
            
            listings.append({
                "listing_id": listing_id,
                "listing_url": full_url,
                "photo_url": photo_url,
                "price": price,
                "location": location,
                "description": description,
                "listing_type": listing_type,
                "Title": f"{location}⎢{price}" if location or price else "",
                "Button1_Label": "Bekijk het op onze website",
                "Button2_Label": f"Contacteer {first_name} - Irres" if first_name else "Contacteer Irres",
                "Button2_email": f"mailto:{email}" if email else ""
            })
        
        return jsonify({"success": True, "count": len(listings), "listings": listings})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "listings": []})

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def root():
    return jsonify({"api": "IRRES.be Listings Scraper", "version": "3.0"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
