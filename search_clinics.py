"""
Google Maps Places API scraper for men's health/TRT/hormone clinics in DFW.

Dependencies:
    pip install requests

Usage:
    export GOOGLE_MAPS_API_KEY="your_key_here"
    python search_clinics.py
"""

import csv
import os
import time
import requests

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
if not API_KEY:
    raise EnvironmentError("GOOGLE_MAPS_API_KEY environment variable is not set.")

SEARCH_TERMS = [
    "men's health clinic",
    "testosterone clinic",
    "TRT clinic",
    "hormone clinic",
]

# Bias results toward Dallas-Fort Worth using a lat/lng and large radius (80 km ~ 50 miles)
DFW_LAT = 32.8998
DFW_LNG = -97.0403
RADIUS_METERS = 80_000

PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
DETAIL_FIELDS = "name,formatted_address,formatted_phone_number,website"


def text_search(query: str) -> list[dict]:
    """Return all place results for a text query, following next_page_token pagination."""
    results = []
    params = {
        "query": query,
        "location": f"{DFW_LAT},{DFW_LNG}",
        "radius": RADIUS_METERS,
        "key": API_KEY,
    }

    while True:
        response = requests.get(PLACES_TEXT_SEARCH_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            print(f"  Warning: Places API status '{status}' for query '{query}'")
            break

        results.extend(data.get("results", []))

        next_token = data.get("next_page_token")
        if not next_token:
            break

        # Google requires a short delay before the next-page token becomes valid
        time.sleep(2)
        params = {"pagetoken": next_token, "key": API_KEY}

    return results


def get_details(place_id: str) -> dict:
    """Fetch phone number and website for a place."""
    params = {
        "place_id": place_id,
        "fields": DETAIL_FIELDS,
        "key": API_KEY,
    }
    response = requests.get(PLACES_DETAILS_URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    return data.get("result", {})


def main():
    seen_place_ids: set[str] = set()
    rows: list[dict] = []

    for term in SEARCH_TERMS:
        print(f"Searching: {term} ...")
        places = text_search(term)
        print(f"  Found {len(places)} raw results")

        for place in places:
            place_id = place.get("place_id")
            name = place.get("name", "")

            # Skip duplicates
            if place_id in seen_place_ids:
                continue
            seen_place_ids.add(place_id)

            # Filter out Gameday (case-insensitive)
            if "gameday" in name.lower():
                print(f"  Filtered (Gameday): {name}")
                continue

            # Fetch details for phone + website
            details = get_details(place_id)

            rows.append(
                {
                    "Name": details.get("name") or name,
                    "Address": details.get("formatted_address")
                    or place.get("formatted_address", ""),
                    "Phone": details.get("formatted_phone_number", ""),
                    "Website": details.get("website", ""),
                }
            )

        # Be polite to the API between search terms
        time.sleep(1)

    output_file = "clinic_leads.csv"
    fieldnames = ["Name", "Address", "Phone", "Website"]

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. {len(rows)} unique clinics written to {output_file}")


if __name__ == "__main__":
    main()
