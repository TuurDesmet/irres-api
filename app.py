import requests
from bs4 import BeautifulSoup
import re
import time
import json

BASE_URL = "https://www.immoweb.be"

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
}

# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def clean_text(text):
    if not text:
        return ""
    return " ".join(text.split()).strip()

def extract_number(text):
    if not text:
        return ""
    m = re.search(r"[\d\.]+", text.replace(",", "."))
    return m.group(0) if m else ""

# ---------------------------------------------------------
# IMAGE FUNCTIONS
# ---------------------------------------------------------

def find_photo_url(link):
    """Try to extract the main image from the listing card (main page)"""
    try:
        res = requests.get(link, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        img_tag = soup.select_one("img.card__picture__image, img.img-fluid")
        if img_tag and img_tag.get("src"):
            return img_tag["src"]
    except:
        pass
    return ""

def find_fallback_image(detail_soup):
    """Extract fallback image from LISTING DETAIL PAGE (the correct place)"""
    if not detail_soup:
        return ""

    # 1) Try OG image
    og = detail_soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og.get("content")

    # 2) Try large gallery image
    gallery = detail_soup.select("img[data-role='gallery-image'], img.gallery__image")
    for g in gallery:
        src = g.get("src") or g.get("data-src")
        if src and "landscape" in src.lower():
            return src

    # 3) Any image from gallery
    for g in gallery:
        src = g.get("src") or g.get("data-src")
        if src:
            return src

    return ""

# ---------------------------------------------------------
# DETAIL PAGE EXTRACTION
# ---------------------------------------------------------

def extract_contact_and_details(detail_url):
    """Extract name, email, details, and fallback image from listing detail page."""
    try:
        res = requests.get(detail_url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
    except:
        return None

    # Extract firstname
    firstname = ""
    name_tag = soup.select_one("p.text-20.font-bold")
    if name_tag:
        firstname = clean_text(name_tag.text)

    # Extract email
    email = ""
    mailtag = soup.select_one("a[href^='mailto:']")
    if mailtag:
        email = mailtag["href"].replace("mailto:", "")

    # Extract DETAILS blocks
    details = {}
    for row in soup.select("div.property__feature, div.classified-table__row"):
        label = row.select_one("span")
        value = row.select_one("div, span.value")
        if not label or not value:
            continue
        key = clean_text(label.text)
        val = clean_text(value.get_text())
        details[key] = val

    # Extract fallback image (from DETAIL PAGE ONLY)
    fallback_image = find_fallback_image(soup)

    return {
        "firstname": firstname,
        "email": email,
        "details": details,
        "fallback_image": fallback_image
    }

# ---------------------------------------------------------
# MAIN LISTINGS SCRAPER
# ---------------------------------------------------------

def get_listings():
    url = "https://www.immoweb.be/nl/zoeken/huis-en-appartement/te-koop?countries=BE&maxPrice=8500000&minPrice=1500000"
    
    print("Fetching main listings page...")
    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.text, "html.parser")

    listings = []

    # Listing cards
    cards = soup.select("a.result-xl, a.result-lg, a.result-md, a.result-sm")

    for card in cards:
        link = BASE_URL + card.get("href", "")
        title = clean_text(card.select_one(".card__title, .result__title").text if card.select_one(".card__title, .result__title") else "")
        location = clean_text(card.select_one(".card__subtitle, .result__subtitle").text if card.select_one(".card__subtitle, .result__subtitle") else "")
        price = clean_text(card.select_one(".card__price, .result__price").text if card.select_one(".card__price, .result__price") else "")
        description = clean_text(card.select_one(".card__description, .result__description").text if card.select_one(".card__description, .result__description") else "")

        # Extract main card image (may fail)
        photo_url = find_photo_url(link)

        # Extract contact + details + fallback image
        detail_data = extract_contact_and_details(link)

        firstname = detail_data["firstname"]
        email = detail_data["email"]
        details = detail_data["details"]
        fallback_image = detail_data["fallback_image"]

        # If main image missing → use fallback image from detail page
        if not photo_url:
            photo_url = fallback_image

        # Build Title, Button1, Button2
        clean_price = extract_number(price)
        main_title = f"{location} ⎢ {clean_price} EUR"

        button1_label = "Bekijk het op onze website"
        button2_label = f"Contacteer {firstname} - Irres" if firstname else "Contacteer Irres"
        button2_email = f"mailto:{email}" if email else ""

        listings.append({
            "listing_id": extract_number(link),
            "listing_url": link,
            "photo_url": photo_url,
            "price": clean_price,
            "location": location,
            "description": description,
            "listing_type": title,
            "Title": main_title,
            "Button1_Label": button1_label,
            "Button2_Label": button2_label,
            "Button2_email": button2_email,
            "details": details
        })

        time.sleep(0.5)  # be nice to the website

    return listings

# ---------------------------------------------------------
# API RESPONSE FORMAT
# ---------------------------------------------------------

def main():
    listings = get_listings()
    return {
        "success": True,
        "listings": listings
    }

if __name__ == "__main__":
    print(json.dumps(main(), indent=2, ensure_ascii=False))
