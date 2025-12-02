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
app.config['JSON_AS_ASCII'] = False

BLACKLIST_IMAGES = [
    "https://irres.be/uploads_c/siteassets/94091/DSCF8677-Edit-Edit-LR_0bcd38e266cca489e4a5ac9ed4b75b78.jpg",
    "https://irres.be/uploads_c/siteassets/89372/013_2021-09-23-081332_hjhi_2211311a508781c4d6535d966f7cc29a.jpg"
]

def normalize_text(s):
    if not s:
        return ""
    s = str(s)
    s = html.unescape(s)
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
    if re.match(r'https?://', src, re.I):
        return src
    return 'https://irres.be/' + src.lstrip('/')

def extract_listing_id(url):
    match = re.search(r'/pand/(\d+)/', url)
    return match.group(1) if match else None

def map_listing_type(english_type):
    mapping = {'Dwelling':'Huis','Flat':'Appartement','Land':'Grond'}
    return mapping.get(english_type, english_type)

def format_price(s):
    s = normalize_text(s)
    if 'Prijs op aanvraag' in s: return 'Prijs op aanvraag'
    if 'Compromis' in s: return 'Compromis in opmaak'
    digits = re.sub(r'[^0-9]', '', s)
    if digits:
        return f'€ {format(int(digits),",").replace(",",".")}'
    return ''

def find_photo_url(link):
    """Try main page photo first"""
    candidates = []
    for img in link.select('img'):
        for attr in ('src','data-src','data-lazy-src','data-original','data-srcset'):
            val = img.get(attr)
            if val:
                if attr in ('srcset','data-srcset'):
                    candidates.extend([p.strip().split()[0] for p in val.split(',')])
                else:
                    candidates.append(val)
    for c in candidates:
        c = normalize_url(c)
        if c and not c.startswith('data:'):
            return c
    return ''

def find_fallback_image(soup):
    """Find first valid landscape property photo, skipping blacklisted or duplicates"""
    candidates = []
    seen = set()
    for img in soup.find_all('img'):
        for attr in ('src','data-src','data-lazy-src','data-original','data-srcset'):
            raw = img.get(attr)
            if not raw: continue
            urls = [raw]
            if attr in ('srcset','data-srcset'):
                urls = [p.strip().split()[0] for p in raw.split(',')]
            for url in urls:
                norm = normalize_url(url)
                if not norm: continue
                if norm in BLACKLIST_IMAGES: continue
                if norm in seen: continue
                seen.add(norm)
                # prefer uploads_c / siteassets
                if re.search(r'/uploads|uploads_c|siteassets', norm, re.I):
                    candidates.append(norm)
    return candidates[0] if candidates else ''

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
        for li in soup.find_all('li', {'data-value': True}):
            val = normalize_text(li.get_text())
            key = li.get('data-value')
            if key == "Terrein oppervlakte": details["Terrein_oppervlakte"]=val
            elif key == "Bewoonbare oppervlakte": details["Bewoonbare_oppervlakte"]=val
            elif key == "Oriëntatie": details["Orientatie"]=val
            elif key == "Slaapkamers": details["Slaapkamers"]=val
            elif key == "Badkamers": details["Badkamers"]=val
            elif key == "Bouwjaar": details["Bouwjaar"]=val
            elif key == "Renovatiejaar": details["Renovatiejaar"]=val
            elif key == "EPC": details["EPC"]=val
            elif key == "Beschikbaarheid": details["Beschikbaarheid"]=val
    except: pass
    return details

def extract_contact_and_details(listing_url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(listing_url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.content,'html.parser')
        first_name = ""
        email_address = ""
        fallback_image = find_fallback_image(soup)
        property_details = extract_property_details(soup)
        # Contact info
        footer = soup.find('form', class_='estate-footer')
        if footer:
            name_p = footer.find('p', class_='font-bold')
            if name_p: first_name = normalize_text(name_p.get_text()).split()[0]
            mail_link = footer.find('a', href=re.compile(r'^mailto:'))
            if mail_link:
                m = re.search(r'mailto:([^\s]+)', mail_link.get('href',''))
                if m: email_address = normalize_text(m.group(1))
        return first_name, email_address, fallback_image, property_details
    except:
        return "", "", "", {}

@app.route('/api/listings', methods=['GET'])
def get_listings():
    try:
        url = "https://irres.be/te-koop"
        headers = {'User-Agent':'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.content,'html.parser')
        listings = []
        for link in soup.find_all('a', href=re.compile(r'/pand/\d+/')):
            listing_url = normalize_text(link.get('href',''))
            if not listing_url: continue
            listing_id = extract_listing_id(listing_url)
            if not listing_id: continue
            full_url = f"https://irres.be{listing_url}" if not listing_url.startswith('http') else listing_url
            text_content = normalize_text(link.get_text(separator='|',strip=True))
            parts = [normalize_text(p) for p in text_content.split('|') if p.strip()]
            location, price, description, listing_type = "", "", "", ""
            for part in parts:
                if '€' in part or 'Prijs op aanvraag' in part or 'Compromis' in part: price=part; continue
                if part in ['Dwelling','Flat','Huis','Appartement','Grond','Land']: listing_type=map_listing_type(part); continue
                if not location: location=part; continue
                if not description and part!=location: description=part
            if not description and len(parts)>2: description=parts[-2] if parts[-1] in ['Dwelling','Flat','Huis','Appartement','Grond','Land'] else parts[-1]
            photo_url = find_photo_url(link)
            time.sleep(0.1)
            first_name,email_address,fallback_image,property_details = extract_contact_and_details(full_url)
            # FIX: use fallback if main photo missing or blacklisted
            if not photo_url or photo_url in BLACKLIST_IMAGES:
                if fallback_image: photo_url=fallback_image
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
                "Button1_Label":"Bekijk het op onze website",
                "Button2_Label": f"Contacteer {first_name} - Irres" if first_name else "Contacteer Irres",
                "Button2_email": f"mailto:{email_address}" if email_address else "",
                "details": property_details
            }
            if location or price or description: listings.append(listing_data)
        # remove duplicates
        seen = set()
        unique=[]
        for l in listings:
            if l['listing_id'] not in seen: seen.add(l['listing_id']); unique.append(l)
        payload={"success":True,"count":len(unique),"listings":unique}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8')
    except Exception as e:
        payload={"success":False,"error":str(e),"listings":[]}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype='application/json; charset=utf-8'), 200

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status":"healthy"})

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "api":"IRRES.be Listings Scraper",
        "version":"3.2",
        "endpoints":{
            "/api/listings":"Get all property listings with contact info and property details",
            "/health":"Health check"
        }
    })

if __name__=='__main__':
    app.run(host='0.0.0.0', port=5000)
