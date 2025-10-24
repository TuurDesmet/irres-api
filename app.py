from flask import Flask, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re

app = Flask(__name__)
CORS(app)  # Enable CORS for Botpress calls

def extract_listing_id(url):
    """Extract listing ID from URL like /pand/8718656/..."""
    match = re.search(r'/pand/(\d+)/', url)
    return match.group(1) if match else None

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
            if not listing_url:
                continue
                
            listing_id = extract_listing_id(listing_url)
            if not listing_id:
                continue
            
            # Full URL
            full_url = f"https://irres.be{listing_url}" if not listing_url.startswith('http') else listing_url
            
            # Extract all text content from the link
            text_content = link.get_text(separator='|', strip=True)
            parts = [p.strip() for p in text_content.split('|') if p.strip()]
            
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
                if photo_src:
                    photo_url = photo_src if photo_src.startswith('http') else f"https://irres.be{photo_src}"
            
            # Create listing object
            listing_data = {
                "listing_id": listing_id,
                "listing_url": full_url,
                "photo_url": photo_url,
                "price": price,
                "location": location,
                "description": description
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
        
        return jsonify({
            "success": True,
            "count": len(unique_listings),
            "listings": unique_listings
        })
    
    except Exception as e:
        # Silent fail - return empty list
        return jsonify({
            "success": False,
            "error": str(e),
            "listings": []
        }), 200  # Return 200 so Botpress doesn't break

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
