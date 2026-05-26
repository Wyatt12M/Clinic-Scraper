"""
Google Maps Places scraper for men's health/TRT/hormone clinics in DFW.
Enriches each clinic with owner contact info via Apollo.io.
Exports to clinic_leads.csv and optionally pushes to Notion.

Dependencies:
    pip install -r requirements.txt

Usage:
    export GOOGLE_MAPS_API_KEY="your_key_here"
    export APOLLO_API_KEY="your_apollo_key_here"
    export NOTION_API_TOKEN="your_notion_token"   # only needed for --notion

    python clinic_scraper.py               # CSV only
    python clinic_scraper.py --notion      # CSV + push to Notion
"""

import argparse
import csv
import os
import time
from urllib.parse import urlparse
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY")
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

APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

OUTPUT_FILE = "clinic_leads.csv"
CSV_FIELDS = [
    "Name", "Address", "Phone", "Website",
    "Owner Name", "Owner Title", "Owner Email", "Owner Phone", "Owner LinkedIn",
]

# Job titles Apollo will match against — ordered from most to least specific
OWNER_TITLES = [
    "Owner",
    "Founder",
    "Co-Founder",
    "CEO",
    "Chief Executive Officer",
    "President",
    "Medical Director",
    "Clinic Director",
]


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
# Apollo.io helpers
# ---------------------------------------------------------------------------

def extract_domain(url: str) -> str | None:
    """Return bare domain from a URL, e.g. 'https://www.example.com/path' -> 'example.com'."""
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.") or None
    except Exception:
        return None


def apollo_find_owner(domain: str | None, company_name: str) -> dict:
    """
    Search Apollo for the highest-ranking owner/founder/exec at a clinic.
    Returns a dict with owner_name, owner_title, owner_email, owner_phone, owner_linkedin.
    Falls back to empty strings if nothing is found.
    """
    empty = {
        "Owner Name": "", "Owner Title": "",
        "Owner Email": "", "Owner Phone": "", "Owner LinkedIn": "",
    }

    if not APOLLO_API_KEY:
        return empty

    payload: dict = {
        "api_key": APOLLO_API_KEY,
        "person_titles": OWNER_TITLES,
        "per_page": 5,
        "page": 1,
    }

    if domain:
        payload["q_organization_domains"] = domain
    else:
        payload["q_keywords"] = company_name

    try:
        resp = requests.post(APOLLO_PEOPLE_SEARCH_URL, json=payload, timeout=15)
        if resp.status_code == 429:
            print("  Apollo rate limit hit — waiting 60 s...")
            time.sleep(60)
            resp = requests.post(APOLLO_PEOPLE_SEARCH_URL, json=payload, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Apollo request failed for '{company_name}': {e}")
        return empty

    people = resp.json().get("people", [])
    if not people:
        return empty

    # Pick the person whose title ranks earliest in OWNER_TITLES
    def title_rank(person: dict) -> int:
        title = (person.get("title") or "").lower()
        for i, t in enumerate(OWNER_TITLES):
            if t.lower() in title:
                return i
        return len(OWNER_TITLES)

    best = min(people, key=title_rank)

    # Phone: Apollo returns a list of phone objects
    phones = best.get("phone_numbers") or []
    phone = phones[0].get("sanitized_number", "") if phones else ""

    return {
        "Owner Name": best.get("name", ""),
        "Owner Title": best.get("title", ""),
        "Owner Email": best.get("email", ""),
        "Owner Phone": phone,
        "Owner LinkedIn": best.get("linkedin_url", ""),
    }


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
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}"
    resp = requests.get(url, headers=notion_headers(), timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch Notion database ({resp.status_code}): {resp.text}")
    return resp.json().get("properties", {})


def ensure_database_properties(existing: dict) -> None:
    """Add any missing properties to the Notion database."""
    needed = {
        "Address": {"rich_text": {}},
        "Phone": {"phone_number": {}},
        "Website": {"url": {}},
        "Source": {"select": {}},
        "Owner Name": {"rich_text": {}},
        "Owner Title": {"rich_text": {}},
        "Owner Email": {"email": {}},
        "Owner Phone": {"phone_number": {}},
        "Owner LinkedIn": {"url": {}},
    }
    to_add = {k: v for k, v in needed.items() if k not in existing}
    if not to_add:
        return

    print(f"  Adding missing Notion properties: {list(to_add.keys())}")
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}"
    resp = requests.patch(url, headers=notion_headers(), json={"properties": to_add}, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to update Notion schema ({resp.status_code}): {resp.text}")


def page_exists(name: str) -> bool:
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}/query"
    payload = {"filter": {"property": "Name", "title": {"equals": name}}}
    resp = requests.post(url, headers=notion_headers(), json=payload, timeout=10)
    if resp.status_code != 200:
        return False
    return len(resp.json().get("results", [])) > 0


def push_to_notion(row: dict, source_term: str) -> None:
    if page_exists(row["Name"]):
        print(f"  Skipping (already in Notion): {row['Name']}")
        return

    properties: dict = {
        "Name": {"title": [{"text": {"content": row["Name"]}}]},
    }

    rich_text_fields = ["Address", "Owner Name", "Owner Title"]
    for field in rich_text_fields:
        if row.get(field):
            properties[field] = {"rich_text": [{"text": {"content": row[field]}}]}

    if row.get("Phone"):
        properties["Phone"] = {"phone_number": row["Phone"]}
    if row.get("Website"):
        properties["Website"] = {"url": row["Website"]}
    if row.get("Owner Email"):
        properties["Owner Email"] = {"email": row["Owner Email"]}
    if row.get("Owner Phone"):
        properties["Owner Phone"] = {"phone_number": row["Owner Phone"]}
    if row.get("Owner LinkedIn"):
        properties["Owner LinkedIn"] = {"url": row["Owner LinkedIn"]}
    if source_term:
        properties["Source"] = {"select": {"name": source_term}}

    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties}
    resp = requests.post(f"{NOTION_API_URL}/pages", headers=notion_headers(), json=payload, timeout=10)
    if resp.status_code != 200:
        print(f"  Error pushing '{row['Name']}' to Notion ({resp.status_code}): {resp.text}")
    else:
        print(f"  Pushed to Notion: {row['Name']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape DFW men's health clinics.")
    parser.add_argument("--notion", action="store_true", help="Push results to Notion.")
    args = parser.parse_args()

    if not GOOGLE_MAPS_API_KEY:
        raise EnvironmentError("GOOGLE_MAPS_API_KEY environment variable is not set.")
    if not APOLLO_API_KEY:
        print("Warning: APOLLO_API_KEY not set — owner contact fields will be empty.")

    if args.notion:
        if not NOTION_API_TOKEN:
            raise EnvironmentError("NOTION_API_TOKEN environment variable is not set.")
        print("Checking Notion database schema...")
        schema = get_database_schema()
        ensure_database_properties(schema)

    seen_place_ids: set[str] = set()
    rows: list[dict] = []
    source_map: dict[str, str] = {}

    # --- Phase 1: Google Maps scrape ---
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
                "Owner Name": "",
                "Owner Title": "",
                "Owner Email": "",
                "Owner Phone": "",
                "Owner LinkedIn": "",
            }
            rows.append(row)
            source_map[row["Name"]] = term

        time.sleep(1)

    print(f"\nFound {len(rows)} unique clinics. Starting owner enrichment via Apollo...")

    # --- Phase 2: Apollo enrichment ---
    for i, row in enumerate(rows, 1):
        print(f"  [{i}/{len(rows)}] {row['Name']}")
        domain = extract_domain(row["Website"])
        owner = apollo_find_owner(domain, row["Name"])
        row.update(owner)
        if owner["Owner Name"]:
            print(f"    -> {owner['Owner Name']} ({owner['Owner Title']}) {owner['Owner Email']}")
        else:
            print("    -> No owner found")
        time.sleep(0.5)  # stay within Apollo rate limits

    # --- Write CSV ---
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} clinics to {OUTPUT_FILE}")

    # --- Push to Notion ---
    if args.notion:
        print("\nPushing to Notion...")
        for row in rows:
            push_to_notion(row, source_map.get(row["Name"], ""))
        print("Notion sync complete.")


if __name__ == "__main__":
    main()
