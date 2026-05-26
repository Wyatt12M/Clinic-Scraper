"""
Google Maps Places scraper for men's health/TRT/hormone clinics in DFW.
Exports results to clinic_leads.csv and optionally pushes to Notion.

Dependencies:
    pip install -r requirements.txt

Usage:
    export GOOGLE_MAPS_API_KEY="your_key_here"
    export NOTION_API_TOKEN="your_notion_token"

    python clinic_scraper.py               # CSV only
    python clinic_scraper.py --notion      # CSV + push to Notion
"""

import argparse
import csv
import os
import time
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
NOTION_API_TOKEN = os.environ.get("NOTION_API_TOKEN")
NOTION_DATABASE_ID = "740eccd78eda458c8e1b83c39787e68f"

SEARCH_TERMS = [
    "men's health clinic",
    "testosterone clinic",
    "TRT clinic",
    "hormone clinic",
]

# Dallas-Fort Worth geographic center; 80 km radius covers the full metro
DFW_LAT = 32.8998
DFW_LNG = -97.0403
RADIUS_METERS = 80_000

PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

OUTPUT_FILE = "clinic_leads.csv"


# ---------------------------------------------------------------------------
# Google Maps helpers
# ---------------------------------------------------------------------------

def text_search(query: str) -> list[dict]:
    """Return all place stubs for a text query, following pagination."""
    results = []
    params = {
        "query": query,
        "location": f"{DFW_LAT},{DFW_LNG}",
        "radius": RADIUS_METERS,
        "key": GOOGLE_MAPS_API_KEY,
    }

    while True:
        resp = requests.get(PLACES_TEXT_SEARCH_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            print(f"  Warning: Places API status '{status}' for query '{query}'")
            break

        results.extend(data.get("results", []))

        next_token = data.get("next_page_token")
        if not next_token:
            break

        time.sleep(2)  # Google requires a brief pause before next_page_token is valid
        params = {"pagetoken": next_token, "key": GOOGLE_MAPS_API_KEY}

    return results


def get_place_details(place_id: str) -> dict:
    """Fetch phone number and website for a place."""
    params = {
        "place_id": place_id,
        "fields": "name,formatted_address,formatted_phone_number,website",
        "key": GOOGLE_MAPS_API_KEY,
    }
    resp = requests.get(PLACES_DETAILS_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", {})


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_database_schema() -> dict:
    """Return the property map for the target database."""
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}"
    resp = requests.get(url, headers=notion_headers(), timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch Notion database ({resp.status_code}): {resp.text}"
        )
    return resp.json().get("properties", {})


def ensure_database_properties(existing: dict) -> None:
    """Add any missing properties (Address, Phone, Website, Source) to the DB."""
    needed = {
        "Address": {"rich_text": {}},
        "Phone": {"phone_number": {}},
        "Website": {"url": {}},
        "Source": {"select": {}},
    }
    to_add = {k: v for k, v in needed.items() if k not in existing}
    if not to_add:
        return

    print(f"  Adding missing Notion properties: {list(to_add.keys())}")
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}"
    resp = requests.patch(
        url,
        headers=notion_headers(),
        json={"properties": to_add},
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to update Notion database schema ({resp.status_code}): {resp.text}"
        )


def page_exists(name: str) -> bool:
    """Return True if a page with this name already exists in the database."""
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "filter": {
            "property": "Name",
            "title": {"equals": name},
        }
    }
    resp = requests.post(url, headers=notion_headers(), json=payload, timeout=10)
    if resp.status_code != 200:
        return False
    return len(resp.json().get("results", [])) > 0


def push_to_notion(row: dict, source_term: str) -> None:
    """Create a page in the Notion database for one clinic row."""
    if page_exists(row["Name"]):
        print(f"  Skipping (already in Notion): {row['Name']}")
        return

    properties: dict = {
        "Name": {"title": [{"text": {"content": row["Name"]}}]},
    }
    if row.get("Address"):
        properties["Address"] = {
            "rich_text": [{"text": {"content": row["Address"]}}]
        }
    if row.get("Phone"):
        properties["Phone"] = {"phone_number": row["Phone"]}
    if row.get("Website"):
        properties["Website"] = {"url": row["Website"]}
    if source_term:
        properties["Source"] = {"select": {"name": source_term}}

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }
    resp = requests.post(
        f"{NOTION_API_URL}/pages",
        headers=notion_headers(),
        json=payload,
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"  Error pushing '{row['Name']}' to Notion ({resp.status_code}): {resp.text}")
    else:
        print(f"  Pushed to Notion: {row['Name']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape DFW men's health clinics.")
    parser.add_argument(
        "--notion",
        action="store_true",
        help="Push results to Notion in addition to saving CSV.",
    )
    args = parser.parse_args()

    if not GOOGLE_MAPS_API_KEY:
        raise EnvironmentError("GOOGLE_MAPS_API_KEY environment variable is not set.")

    if args.notion:
        if not NOTION_API_TOKEN:
            raise EnvironmentError("NOTION_API_TOKEN environment variable is not set.")
        print("Checking Notion database schema...")
        schema = get_database_schema()
        ensure_database_properties(schema)

    seen_place_ids: set[str] = set()
    rows: list[dict] = []
    source_map: dict[str, str] = {}  # name -> first search term that found it

    for term in SEARCH_TERMS:
        print(f"\nSearching: {term} ...")
        places = text_search(term)
        print(f"  {len(places)} raw results")

        for place in places:
            place_id = place.get("place_id")
            name = place.get("name", "")

            if place_id in seen_place_ids:
                continue
            seen_place_ids.add(place_id)

            if "gameday" in name.lower():
                print(f"  Filtered (Gameday): {name}")
                continue

            details = get_place_details(place_id)
            row = {
                "Name": details.get("name") or name,
                "Address": details.get("formatted_address") or place.get("formatted_address", ""),
                "Phone": details.get("formatted_phone_number", ""),
                "Website": details.get("website", ""),
            }
            rows.append(row)
            source_map[row["Name"]] = term

        time.sleep(1)

    # Write CSV
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "Address", "Phone", "Website"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} clinics to {OUTPUT_FILE}")

    # Push to Notion
    if args.notion:
        print("\nPushing to Notion...")
        for row in rows:
            push_to_notion(row, source_map.get(row["Name"], ""))
        print("Notion sync complete.")


if __name__ == "__main__":
    main()
