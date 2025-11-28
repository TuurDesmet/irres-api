import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.irres.be"


def scrape_listing_details(detail_url):
    """Visit detail page and extract name + email + first name."""
    response = requests.get(detail_url)
    detail_soup = BeautifulSoup(response.text, "html.parser")

    contact_block = detail_soup.select_one(
        "div.leading-7.ml-10.flex.flex-col.justify-between"
    )

    # Extract name
    name_tag = contact_block.select_one("p.-mt-1.text-20.font-bold")
    name = name_tag.get_text(strip=True) if name_tag else ""

    # Extract email
    email_tag = contact_block.select_one("a[href^='mailto:']")
    email = email_tag.get("href").replace("mailto:", "") if email_tag else ""

    # Extract first name
    first_name = name.split(" ")[0] if name else ""

    return {
        "name": name,
        "email": email,
        "first_name": first_name
    }


def convert_price(price_str):
    """Convert price string like '€ 350.000' into an integer number."""
    return int(price_str.replace("€", "").replace(".", "").replace(" ", "").strip())


def scrape_listings():
    url = f"{BASE_URL}/te-koop"
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    listings = []

    for item in soup.select("a.flex.flex-col"):        
        # Required original fields
        listing_id = item.get("id") or ""  # If ID is on the <a> tag
        relative_url = item.get("href")
        listing_url = BASE_URL + relative_url

        # Photo
        photo_tag = item.select_one("img")
        photo_url = photo_tag.get("src") if photo_tag else ""

        # Price
        price_tag = item.select_one("p.text-20.font-bold")
        price_str = price_tag.get_text(strip=True) if price_tag else "€ 0"
        price_value = convert_price(price_str)

        # Location
        location_tag = item.select_one("p.text-14.font-bold.uppercase")
        location = location_tag.get_text(strip=True) if location_tag else ""

        # Description
        desc_tag = item.select_one("p.text-14:not(.font-bold)")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        # Listing type (house, apartment…)
        type_tag = item.select_one("p.text-12.uppercase")
        listing_type = type_tag.get_text(strip=True) if type_tag else ""

        # Visit detail page
        contact_info = scrape_listing_details(listing_url)

        first_name = contact_info["first_name"]
        email = contact_info["email"]

        # NEW FIELDS (your specifications)
        Button1_Label = "Bekijk het op onze website"
        Title = f"{location}⎢{price_str}"
        Button2_Label = f"Contacteer {first_name} - Irres"
        Button2_Email = f"mailto:{email}"

        listings.append({
            # ORIGINAL FIELD NAMES (unchanged)
            "listing_id": listing_id,
            "listing_url": listing_url,
            "photo_url": photo_url,
            "price": price_value,
            "location": location,
            "description": description,
            "listing_type": listing_type,

            # NEW FIELDS YOU REQUESTED
            "title": Title,
            "button1_label": Button1_Label,
            "button2_label": Button2_Label,
            "button2_email": Button2_Email,

            # Contact fields (not renamed)
            "contact_name": contact_info["name"],
            "contact_email": contact_info["email"],
            "contact_first_name": contact_info["first_name"],
        })

    return listings


if __name__ == "__main__":
    data = scrape_listings()
    for d in data:
        print(d)