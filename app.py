"""
IRRES Real Estate Scraper API
Professional web scraper for extracting property listings from irres.be
Author: Professional Coder with 25 years experience
"""

import requests
from bs4 import BeautifulSoup
import json
import re
from typing import Dict, List, Optional
import html


class IRRESScraper:
    """Main scraper class for IRRES real estate website"""
    
    def __init__(self):
        self.base_url = "https://irres.be"
        self.listings_url = f"{self.base_url}/te-koop"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    
    def get_all_listings(self) -> List[Dict]:
        """
        Main function to get all listings from the website
        Returns a list of complete listing dictionaries
        """
        print("Starting to scrape IRRES listings...")
        
        # Step 1: Get all listing URLs from main page
        listing_previews = self._get_listing_previews()
        print(f"Found {len(listing_previews)} listings on main page")
        
        # Step 2: Get detailed information for each listing
        complete_listings = []
        for i, preview in enumerate(listing_previews, 1):
            print(f"Processing listing {i}/{len(listing_previews)}: {preview['listing_id']}")
            try:
                complete_listing = self._get_complete_listing(preview)
                complete_listings.append(complete_listing)
            except Exception as e:
                print(f"Error processing listing {preview['listing_id']}: {str(e)}")
                continue
        
        return complete_listings
    
    def _get_listing_previews(self) -> List[Dict]:
        """
        Step 1: Extract basic listing information from main page
        """
        response = requests.get(self.listings_url, headers=self.headers)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        listings = []
        listing_blocks = soup.find_all('div', class_='inner-container')
        
        for block in listing_blocks:
            # Find the link with name attribute (contains listing_id)
            link = block.find('a', attrs={'name': True})
            if not link:
                continue
            
            listing_id = link.get('name', '')
            listing_url = self.base_url + link.get('href', '')
            
            # Extract location from the estate-city span
            location_elem = block.find('h2', class_='estate-city')
            location = self._extract_text(location_elem) if location_elem else ''
            location = location.split('|')[0].strip() if '|' in location else location.strip()
            
            # Extract price from estate-price span
            price_elem = block.find('span', class_='estate-price')
            price = self._extract_text(price_elem) if price_elem else ''
            price = self._clean_price(price)
            
            # Extract description
            description_elem = block.find('p', class_='text-18')
            description = description_elem.get_text(strip=True) if description_elem else ''
            
            # Extract listing type
            type_elem = block.find('p', class_='estate-type')
            listing_type = type_elem.get_text(strip=True) if type_elem else ''
            listing_type = self._translate_type(listing_type)
            
            # Extract photo_url (images only, no videos)
            photo_url = self._extract_photo_from_block(block)
            
            # Create title
            title = f"{location}⎥{price}"
            
            # Button labels
            button1_label = "Bekijk het op onze website"
            button3_label = "Vraag prijs aan" if price == "Prijs op aanvraag" else ""
            button3_field = ""  # Will be filled with email later
            
            listings.append({
                'listing_id': listing_id,
                'listing_url': listing_url,
                'photo_url': photo_url,
                'price': price,
                'location': location,
                'description': description,
                'listing_type': listing_type,
                'title': title,
                'button1_label': button1_label,
                'button3_label': button3_label,
                'button3_field': button3_field
            })
        
        return listings
    
    def _get_complete_listing(self, preview: Dict) -> Dict:
        """
        Step 2: Get complete listing details from individual listing page
        """
        response = requests.get(preview['listing_url'], headers=self.headers)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # If photo_url not found on main page, find it here
        if not preview['photo_url']:
            main_container = soup.find('main', attrs={'data-barba-namespace': 'estate'})
            if main_container:
                preview['photo_url'] = self._extract_photo_from_detail(main_container)
        
        # Extract Button2 info (contact form email)
        button2_label, button2_email = self._extract_contact_info(soup)
        
        # Extract details (Kenmerken section)
        details = self._extract_details(soup)
        
        # Update button3_field if needed
        if preview['button3_label']:
            preview['button3_field'] = button2_email
        
        # Combine all information
        complete_listing = {
            **preview,
            'button2_label': button2_label,
            'button2_email': button2_email,
            'details': details
        }
        
        return complete_listing
    
    def _extract_photo_from_block(self, block) -> str:
        """Extract photo URL (images only, no videos) from listing block on main page"""
        # Look for picture/img tags (skip videos completely)
        picture = block.find('picture')
        if picture:
            img = picture.find('img')
            if img:
                srcset = img.get('srcset', '')
                if srcset and 'uploads_c' in srcset:
                    # Extract the URL from srcset
                    urls = [url.strip().split()[0] for url in srcset.split(',')]
                    for url in urls:
                        if url.startswith('http'):
                            return url
                        elif url.startswith('/'):
                            return self.base_url + url
        
        return ''
    
    def _extract_photo_from_detail(self, main_container) -> str:
        """Extract photo URL (images only, no videos) from detail page"""
        # Look for the first proper image in the main container (skip videos)
        pictures = main_container.find_all('picture')
        
        for picture in pictures:
            img = picture.find('img')
            if img:
                srcset = img.get('srcset', '')
                if srcset and 'uploads_c' in srcset:
                    urls = [url.strip().split()[0] for url in srcset.split(',')]
                    # Get the highest quality image URL
                    for url in urls:
                        if 'http' in url:
                            return url
                        elif url.startswith('/'):
                            return self.base_url + url
        
        return ''
    
    def _extract_contact_info(self, soup) -> tuple:
        """Extract contact information from the listing page"""
        # Look for the contact section in footer
        form = soup.find('form', id='footer-form')
        if not form:
            return '', ''
        
        # Find the team member info
        email_link = form.find('a', href=re.compile(r'mailto:'))
        if email_link:
            email = email_link.get_text(strip=True)
            button2_email = f"mailto:{email}"
            
            # Extract name (everything before the @)
            name_part = email.split('@')[0] if '@' in email else ''
            
            # Find the full name from the paragraph
            name_elem = form.find('p', class_='font-bold')
            if name_elem:
                full_name = name_elem.get_text(strip=True)
                button2_label = f"Contacteer {full_name}"
            else:
                button2_label = f"Contacteer {name_part.title()} - Irres"
            
            return button2_label, button2_email
        
        return '', ''
    
    def _extract_details(self, soup) -> Dict:
        """Extract property details from the Kenmerken section"""
        details = {
            'Terrein_oppervlakte': '',
            'Bewoonbare_oppervlakte': '',
            'Orientatie': '',
            'Slaapkamers': '',
            'Badkamers': '',
            'Bouwjaar': '',
            'Renovatiejaar': '',
            'EPC': '',
            'Beschikbaarheid': ''
        }
        
        # Find the Kenmerken section
        kenmerken_section = soup.find('h2', string=re.compile(r'Kenmerken'))
        if not kenmerken_section:
            return details
        
        # Find the ul containing the details
        ul = kenmerken_section.find_next('ul')
        if not ul:
            return details
        
        # Extract each detail
        list_items = ul.find_all('li', class_='item-hover-text')
        
        for item in list_items:
            data_value = item.get('data-value', '')
            value_elem = item.find('p', class_='pl-6')
            
            if not value_elem:
                continue
            
            value = value_elem.get_text(strip=True)
            value = self._decode_html(value)
            
            # Map to our detail keys
            if 'Terrein oppervlakte' in data_value:
                details['Terrein_oppervlakte'] = value
            elif 'Bewoonbare oppervlakte' in data_value:
                details['Bewoonbare_oppervlakte'] = value
            elif 'Oriëntatie' in data_value or 'Orientatie' in data_value:
                details['Orientatie'] = value
            elif 'Slaapkamers' in data_value:
                details['Slaapkamers'] = value
            elif 'Badkamers' in data_value:
                details['Badkamers'] = value
            elif 'Bouwjaar' in data_value:
                details['Bouwjaar'] = value
            elif 'Renovatiejaar' in data_value:
                details['Renovatiejaar'] = value
            elif 'EPC' in data_value:
                details['EPC'] = value
            elif 'Beschikbaarheid' in data_value:
                details['Beschikbaarheid'] = value
        
        return details
    
    def _extract_text(self, element) -> str:
        """Extract clean text from an element, handling nested divs"""
        if not element:
            return ''
        
        # Get all text and clean it
        text = element.get_text(separator='', strip=True)
        text = self._decode_html(text)
        return text
    
    def _clean_price(self, price: str) -> str:
        """Clean and format price string"""
        # Decode HTML entities
        price = self._decode_html(price)
        price = price.strip()
        
        # Already in correct format
        return price
    
    def _translate_type(self, listing_type: str) -> str:
        """Translate listing type to Dutch"""
        translations = {
            'Dwelling': 'Huis',
            'Flat': 'Appartement',
            'Land': 'Grond'
        }
        return translations.get(listing_type, listing_type)
    
    def _decode_html(self, text: str) -> str:
        """Decode HTML entities like &#178; to proper characters"""
        text = html.unescape(text)
        # Replace common entities
        text = text.replace('&#178;', '²')
        text = text.replace('&sup2;', '²')
        text = text.replace('&#8364;', '€')
        return text
    
    def save_to_json(self, listings: List[Dict], filename: str = 'irres_listings.json'):
        """Save listings to JSON file"""
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(listings, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(listings)} listings to {filename}")


# =======================
# USAGE EXAMPLE
# =======================

def main():
    """Main execution function"""
    
    # Initialize scraper
    scraper = IRRESScraper()
    
    # Get all listings
    listings = scraper.get_all_listings()
    
    # Save to JSON file
    scraper.save_to_json(listings)
    
    # Print example of first listing
    if listings:
        print("\n" + "="*50)
        print("EXAMPLE OUTPUT - First Listing:")
        print("="*50)
        print(json.dumps(listings[0], ensure_ascii=False, indent=2))
    
    return listings


if __name__ == "__main__":
    listings = main()
