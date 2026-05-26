#!/usr/bin/env python3
"""
Northarion Partners — Men's Health Clinic Lead Scraper

Finds independent cash-pay men's health clinics across 25 US cities via
Google Maps, enriches with Apollo.io owner contacts, falls back to website
scraping, and exports to a dated CSV with optional Notion push.

Setup:
    1. Copy .env.example to .env and fill in your API keys
    2. pip install -r requirements.txt
    3. python clinic_scraper.py [options]

Options:
    --notion                 Push results to Notion database
    --max-per-city N         Limit clinics per city (default: unlimited)
    --cities "Dallas TX" ... Scrape specific cities only
    --apollo-only            Skip website scraping fallback
"""

import argparse
import csv
import logging
import os
import re
import time
from datetime import date
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename="scraper_errors.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
NOTION_API_TOKEN = os.environ.get("NOTION_API_TOKEN", "")
NOTION_DATABASE_ID = "2bded5ba-ce90-42b5-a9e4-7323eef79988"

# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------
SEARCH_QUERIES = [
    "men's health clinic",
    "TRT clinic",
    "testosterone clinic",
    "hormone therapy clinic men",
    "men's wellness clinic",
]

CITIES: list[tuple[str, str, float, float]] = [
    ("Dallas",        "TX", 32.7767,  -96.7970),
    ("Houston",       "TX", 29.7604,  -95.3698),
    ("Austin",        "TX", 30.2672,  -97.7431),
    ("San Antonio",   "TX", 29.4241,  -98.4936),
    ("Nashville",     "TN", 36.1627,  -86.7816),
    ("Charlotte",     "NC", 35.2271,  -80.8431),
    ("Atlanta",       "GA", 33.7490,  -84.3880),
    ("Phoenix",       "AZ", 33.4484, -112.0740),
    ("Denver",        "CO", 39.7392, -104.9903),
    ("Las Vegas",     "NV", 36.1699, -115.1398),
    ("Tampa",         "FL", 27.9506,  -82.4572),
    ("Orlando",       "FL", 28.5383,  -81.3792),
    ("Oklahoma City", "OK", 35.4676,  -97.5164),
    ("Tulsa",         "OK", 36.1540,  -95.9928),
    ("Kansas City",   "MO", 39.0997,  -94.5786),
    ("Indianapolis",  "IN", 39.7684,  -86.1581),
    ("Columbus",      "OH", 39.9612,  -82.9988),
    ("Pittsburgh",    "PA", 40.4406,  -79.9959),
    ("Raleigh",       "NC", 35.7796,  -78.6382),
    ("Salt Lake City","UT", 40.7608, -111.8910),
    ("Boise",         "ID", 43.6150, -116.2023),
    ("Albuquerque",   "NM", 35.0844, -106.6504),
    ("Tucson",        "AZ", 32.2226, -110.9747),
    ("Memphis",       "TN", 35.1495,  -90.0490),
    ("Birmingham",    "AL", 33.5186,  -86.8104),
]

FRANCHISE_KEYWORDS = [
    "gameday", "low t center", "vitality md", "defy medical",
    "maximus", "fountain", "hims", "ro health", "hone health",
    "keep", "peter md", "regenics", "livewell",
]

OWNER_TITLES = [
    "Owner", "Founder", "Co-Founder", "CEO", "Chief Executive Officer",
    "Clinic Director", "Medical Director", "Practice Manager", "President",
    "Managing Partner", "CFO", "Chief Financial Officer", "Administrator",
]

WEBSITE_PATHS = [
    "/about", "/about-us", "/team", "/our-team",
    "/contact", "/contact-us", "/meet-the-team", "/staff", "/leadership",
]

RADIUS_METERS = 50_000

PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL     = "https://maps.googleapis.com/maps/api/place/details/json"
APOLLO_SEARCH_URL      = "https://api.apollo.io/v1/mixed_people/search"
NOTION_API_URL         = "https://api.notion.com/v1"
NOTION_VERSION         = "2022-06-28"

OUTPUT_FILE = f"clinic_leads_{date.today().strftime('%Y%m%d')}.csv"
TODAY       = date.today().isoformat()

CSV_FIELDS = [
    "Clinic Name", "Website", "Clinic Phone", "Address", "City", "State",
    "Google Rating", "Review Count",
    "Owner Name", "Owner Title", "Owner Direct Email", "Owner Direct Phone", "Owner LinkedIn",
    "Company LinkedIn Search",
    "Email Guess 1", "Email Guess 2", "Email Guess 3",
    "Apollo Verified", "Source", "Date Scraped",
]

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
NAME_RE  = re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})\b")

_apollo_call_times: list[float] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_franchise(name: str, website: str = "") -> bool:
    text = (name + " " + website).lower()
    return any(kw in text for kw in FRANCHISE_KEYWORDS)


def extract_domain(url: str) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.") or None
    except Exception:
        return None


def safe_get(url: str, timeout: int = 8, **kwargs) -> requests.Response | None:
    try:
        resp = requests.get(url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logging.error("GET %s: %s", url, e)
        return None


def safe_post(url: str, timeout: int = 15, **kwargs) -> requests.Response | None:
    try:
        resp = requests.post(url, timeout=timeout, **kwargs)
        return resp
    except requests.RequestException as e:
        logging.error("POST %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Google Maps
# ---------------------------------------------------------------------------

def maps_text_search(query: str, lat: float, lng: float) -> list[dict]:
    results = []
    params = {
        "query": query,
        "location": f"{lat},{lng}",
        "radius": RADIUS_METERS,
        "key": GOOGLE_MAPS_API_KEY,
    }
    while True:
        resp = safe_get(PLACES_TEXT_SEARCH_URL, params=params)
        if not resp:
            break
        data = resp.json()
        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            logging.error("Maps status '%s' for query '%s'", status, query)
            break
        results.extend(data.get("results", []))
        token = data.get("next_page_token")
        if not token:
            break
        time.sleep(2)
        params = {"pagetoken": token, "key": GOOGLE_MAPS_API_KEY}
    time.sleep(1.5)
    return results


def maps_place_details(place_id: str) -> dict:
    params = {
        "place_id": place_id,
        "fields": "name,formatted_address,formatted_phone_number,website,rating,user_ratings_total",
        "key": GOOGLE_MAPS_API_KEY,
    }
    resp = safe_get(PLACES_DETAILS_URL, params=params)
    return resp.json().get("result", {}) if resp else {}


# ---------------------------------------------------------------------------
# Apollo.io
# ---------------------------------------------------------------------------

def _apollo_rate_limit() -> None:
    """Stay under 50 calls/minute."""
    now = time.time()
    window = [t for t in _apollo_call_times if now - t < 60]
    _apollo_call_times[:] = window
    if len(window) >= 50:
        sleep_for = 60 - (now - window[0]) + 1
        print(f"  Apollo rate limit — waiting {sleep_for:.0f}s...")
        time.sleep(sleep_for)
    _apollo_call_times.append(time.time())


def apollo_find_owner(domain: str | None, company_name: str) -> dict:
    empty = {
        "Owner Name": "", "Owner Title": "",
        "Owner Direct Email": "", "Owner Direct Phone": "", "Owner LinkedIn": "",
        "Apollo Verified": "No",
    }
    if not APOLLO_API_KEY:
        return empty

    _apollo_rate_limit()

    payload: dict = {
        "api_key": APOLLO_API_KEY,
        "person_titles": OWNER_TITLES,
        "per_page": 5,
        "page": 1,
    }
    if domain:
        payload["q_organization_domains"] = [domain]
    else:
        payload["q_keywords"] = company_name

    resp = safe_post(APOLLO_SEARCH_URL, json=payload)

    if resp is not None and resp.status_code == 429:
        print("  Apollo 429 — waiting 10s and retrying...")
        time.sleep(10)
        _apollo_rate_limit()
        resp = safe_post(APOLLO_SEARCH_URL, json=payload)

    if not resp or resp.status_code != 200:
        logging.error("Apollo non-200 for '%s': %s", company_name,
                      resp.status_code if resp else "no response")
        return empty

    people = resp.json().get("people", [])
    if not people:
        return empty

    def title_rank(p: dict) -> int:
        t = (p.get("title") or "").lower()
        for i, title in enumerate(OWNER_TITLES):
            if title.lower() in t:
                return i
        return len(OWNER_TITLES)

    best = min(people, key=title_rank)
    phones = best.get("phone_numbers") or []
    phone = phones[0].get("sanitized_number", "") if phones else ""

    time.sleep(1)
    return {
        "Owner Name": best.get("name", ""),
        "Owner Title": best.get("title", ""),
        "Owner Direct Email": best.get("email", ""),
        "Owner Direct Phone": phone,
        "Owner LinkedIn": best.get("linkedin_url", ""),
        "Apollo Verified": "Yes",
    }


# ---------------------------------------------------------------------------
# Website scraper fallback
# ---------------------------------------------------------------------------

def _fetch_page_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LeadScraper/1.0)"}
    resp = safe_get(url, timeout=8, headers=headers)
    if not resp:
        return ""
    try:
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception as e:
        logging.error("Parse error %s: %s", url, e)
        return ""


def _find_owner_name_near_title(text: str) -> tuple[str, str]:
    """Return (name, title) by finding a decision-maker title and nearby capitalized name."""
    for title in OWNER_TITLES:
        pattern = re.compile(
            r"([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})"   # name before title
            r"(?:\s*,?\s*" + re.escape(title) + r")"
            r"|"
            r"(?:" + re.escape(title) + r"\s*:?\s*)"  # title before name
            r"([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})",
            re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            name = (m.group(1) or m.group(2) or "").strip()
            if name:
                return name, title
    return "", ""


def scrape_website_contacts(base_url: str) -> dict:
    empty = {
        "Owner Name": "", "Owner Title": "",
        "Owner Direct Email": "", "Owner Direct Phone": "",
    }
    if not base_url:
        return empty

    base = base_url.rstrip("/")
    all_text = ""

    for path in WEBSITE_PATHS:
        url = base + path
        text = _fetch_page_text(url)
        if text:
            all_text += " " + text
        time.sleep(0.5)

    if not all_text:
        return empty

    emails = EMAIL_RE.findall(all_text)
    phones = PHONE_RE.findall(all_text)
    name, title = _find_owner_name_near_title(all_text)

    # Filter out generic/noreply emails
    personal_emails = [
        e for e in emails
        if not any(g in e.lower() for g in ("noreply", "info@", "hello@", "contact@", "admin@"))
    ]

    return {
        "Owner Name": name,
        "Owner Title": title,
        "Owner Direct Email": personal_emails[0] if personal_emails else (emails[0] if emails else ""),
        "Owner Direct Phone": phones[0] if phones else "",
    }


# ---------------------------------------------------------------------------
# Email guesser
# ---------------------------------------------------------------------------

def guess_emails(full_name: str, domain: str | None) -> tuple[str, str, str]:
    if not domain or not full_name:
        return "", "", ""
    parts = full_name.strip().lower().split()
    if len(parts) < 2:
        return f"{parts[0]}@{domain}", "", f"info@{domain}"
    first, last = parts[0], parts[-1]
    return (
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{first[0]}{last}@{domain}",
    )


# ---------------------------------------------------------------------------
# LinkedIn URL builder
# ---------------------------------------------------------------------------

def linkedin_owner_url(clinic_name: str) -> str:
    q = quote_plus(f"{clinic_name} owner OR founder OR director")
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


def linkedin_company_url(clinic_name: str) -> str:
    q = quote_plus(clinic_name)
    return f"https://www.linkedin.com/search/results/companies/?keywords={q}"


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_ensure_schema() -> None:
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}"
    resp = requests.get(url, headers=_notion_headers(), timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Cannot reach Notion database ({resp.status_code}): {resp.text}")

    existing = resp.json().get("properties", {})
    needed = {
        "Contact Name":  {"rich_text": {}},
        "Email":         {"email": {}},
        "Phone":         {"phone_number": {}},
        "Website":       {"url": {}},
        "Location":      {"rich_text": {}},
        "LinkedIn":      {"url": {}},
        "Stage":         {"select": {}},
        "Priority":      {"select": {}},
        "Source":        {"select": {}},
        "Clinic Phone":  {"phone_number": {}},
    }
    to_add = {k: v for k, v in needed.items() if k not in existing}
    if not to_add:
        return
    print(f"  Adding Notion properties: {list(to_add.keys())}")
    requests.patch(url, headers=_notion_headers(),
                   json={"properties": to_add}, timeout=10)


def notion_page_exists(clinic_name: str) -> bool:
    url = f"{NOTION_API_URL}/databases/{NOTION_DATABASE_ID}/query"
    payload = {"filter": {"property": "title", "title": {"equals": clinic_name}}}
    resp = requests.post(url, headers=_notion_headers(), json=payload, timeout=10)
    if resp.status_code != 200:
        return False
    return len(resp.json().get("results", [])) > 0


def notion_push(row: dict) -> None:
    if notion_page_exists(row["Clinic Name"]):
        print(f"  Skipping (exists): {row['Clinic Name']}")
        return

    props: dict = {
        "title": {"title": [{"text": {"content": row["Clinic Name"]}}]},
        "Stage":    {"select": {"name": "New Lead"}},
        "Priority": {"select": {"name": "A - Hot"}},
        "Source":   {"select": {"name": "Clinic Scraper"}},
    }

    def rt(val: str) -> dict:
        return {"rich_text": [{"text": {"content": val}}]}

    if row.get("Contact Name") or row.get("Owner Name"):
        props["Contact Name"] = rt(row.get("Owner Name", ""))
    if row.get("Owner Direct Email"):
        props["Email"] = {"email": row["Owner Direct Email"]}
    if row.get("Owner Direct Phone"):
        props["Phone"] = {"phone_number": row["Owner Direct Phone"]}
    if row.get("Website"):
        props["Website"] = {"url": row["Website"]}
    if row.get("City") and row.get("State"):
        props["Location"] = rt(f"{row['City']}, {row['State']}")
    if row.get("Owner LinkedIn"):
        props["LinkedIn"] = {"url": row["Owner LinkedIn"]}
    if row.get("Clinic Phone"):
        props["Clinic Phone"] = {"phone_number": row["Clinic Phone"]}

    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}
    resp = requests.post(f"{NOTION_API_URL}/pages", headers=_notion_headers(),
                         json=payload, timeout=10)
    if resp.status_code != 200:
        logging.error("Notion push failed for '%s': %s", row["Clinic Name"], resp.text)
        print(f"  Notion error for '{row['Clinic Name']}': {resp.status_code}")
    else:
        print(f"  Pushed to Notion: {row['Clinic Name']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_row(
    place: dict,
    details: dict,
    city: str,
    state: str,
    owner: dict,
    source: str,
    apollo_only: bool,
) -> dict:
    name    = details.get("name") or place.get("name", "")
    website = details.get("website", "")
    domain  = extract_domain(website)

    # Website scrape fallback
    if not apollo_only and not owner.get("Owner Name") and website:
        scraped = scrape_website_contacts(website)
        if scraped.get("Owner Name") or scraped.get("Owner Direct Email"):
            owner.update(scraped)
            source = "Website Scrape"

    # Email guesses
    g1, g2, g3 = guess_emails(owner.get("Owner Name", ""), domain)

    return {
        "Clinic Name":        name,
        "Website":            website,
        "Clinic Phone":       details.get("formatted_phone_number", ""),
        "Address":            details.get("formatted_address") or place.get("formatted_address", ""),
        "City":               city,
        "State":              state,
        "Google Rating":      details.get("rating", ""),
        "Review Count":       details.get("user_ratings_total", ""),
        "Owner Name":         owner.get("Owner Name", ""),
        "Owner Title":        owner.get("Owner Title", ""),
        "Owner Direct Email": owner.get("Owner Direct Email", ""),
        "Owner Direct Phone": owner.get("Owner Direct Phone", ""),
        "Owner LinkedIn":     owner.get("Owner LinkedIn", ""),
        "Company LinkedIn Search": linkedin_company_url(name),
        "Email Guess 1":      g1,
        "Email Guess 2":      g2,
        "Email Guess 3":      g3,
        "Apollo Verified":    owner.get("Apollo Verified", "No"),
        "Source":             source,
        "Date Scraped":       TODAY,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Northarion Partners clinic scraper.")
    parser.add_argument("--notion",       action="store_true", help="Push to Notion")
    parser.add_argument("--max-per-city", type=int,  default=0,  metavar="N")
    parser.add_argument("--cities",       nargs="+", default=[],  metavar="CITY_ST")
    parser.add_argument("--apollo-only",  action="store_true", help="Skip website scraping")
    args = parser.parse_args()

    if not GOOGLE_MAPS_API_KEY:
        raise EnvironmentError("GOOGLE_MAPS_API_KEY not set in .env")
    if not APOLLO_API_KEY:
        print("Warning: APOLLO_API_KEY not set — owner fields will be empty.")

    if args.notion:
        if not NOTION_API_TOKEN:
            raise EnvironmentError("NOTION_API_TOKEN not set in .env")
        print("Verifying Notion database...")
        notion_ensure_schema()

    # Filter cities if --cities was passed
    target_cities = CITIES
    if args.cities:
        requested = {c.lower() for c in args.cities}
        target_cities = [
            c for c in CITIES
            if f"{c[0]} {c[1]}".lower() in requested or c[0].lower() in requested
        ]
        if not target_cities:
            print("No matching cities found. Use format: --cities \"Dallas TX\" \"Austin TX\"")
            return

    seen_place_ids: set[str] = set()
    rows: list[dict] = []

    for city, state, lat, lng in target_cities:
        city_count = 0
        print(f"\n--- {city}, {state} ---")

        for query in SEARCH_QUERIES:
            if args.max_per_city and city_count >= args.max_per_city:
                break
            print(f"  Searching: {query}")

            try:
                places = maps_text_search(query, lat, lng)
            except Exception as e:
                logging.error("Maps search failed %s / %s: %s", city, query, e)
                continue

            for place in places:
                if args.max_per_city and city_count >= args.max_per_city:
                    break

                place_id = place.get("place_id")
                raw_name = place.get("name", "")

                if place_id in seen_place_ids:
                    continue
                seen_place_ids.add(place_id)

                if is_franchise(raw_name):
                    print(f"    Filtered (franchise): {raw_name}")
                    continue

                try:
                    details = maps_place_details(place_id)
                    full_name = details.get("name") or raw_name
                    website   = details.get("website", "")

                    if is_franchise(full_name, website):
                        print(f"    Filtered (franchise): {full_name}")
                        continue

                    domain = extract_domain(website)
                    print(f"    {full_name} ({domain or 'no website'})")

                    owner  = apollo_find_owner(domain, full_name)
                    source = "Apollo" if owner.get("Apollo Verified") == "Yes" else "Google Only"

                    row = build_row(place, details, city, state, owner, source, args.apollo_only)
                    rows.append(row)
                    city_count += 1

                    if owner.get("Owner Name"):
                        print(f"      -> {owner['Owner Name']} | {owner.get('Owner Direct Email', '')}")

                except Exception as e:
                    logging.error("Failed on place '%s': %s", raw_name, e)
                    print(f"    Error on '{raw_name}' — logged, continuing.")

    # Write CSV
    print(f"\nWriting {len(rows)} rows to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved: {OUTPUT_FILE}")

    # Push to Notion
    if args.notion:
        print(f"\nPushing {len(rows)} records to Notion...")
        for row in rows:
            try:
                notion_push(row)
            except Exception as e:
                logging.error("Notion push error for '%s': %s", row.get("Clinic Name"), e)
        print("Notion sync complete.")


if __name__ == "__main__":
    main()
