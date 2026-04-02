"""
Kurve Apartment Price Tracker — Scraper
----------------------------------------
Scrapes https://www.apartments.com/kurve-los-angeles-ca/047tn61/ and appends
a new snapshot to kurve_data.json.

Requirements:
    pip install playwright
    playwright install chromium

Usage:
    python scraper.py
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
PROPERTY_URL  = "https://www.apartments.com/kurve-los-angeles-ca/047tn61/"
DATA_FILE     = Path(__file__).parent / "kurve_data.json"
PST           = timezone(timedelta(hours=-7))   # PDT; use -8 in winter


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean_price(text: str) -> int | None:
    """'$2,149/mo' → 2149"""
    m = re.search(r"\$?([\d,]+)", text.replace(",", ""))
    return int(m.group(1)) if m else None


def clean_sqft(text: str) -> int | None:
    """'529 sq ft' → 529"""
    m = re.search(r"([\d,]+)", text.replace(",", ""))
    return int(m.group(1)) if m else None


def parse_availability(text: str) -> str:
    """Normalise availability text to 'Now', 'Apr 5', 'May 27', etc."""
    t = text.strip()
    if not t or t.lower() in ("available", "available now", "now"):
        return "Now"
    # Strip leading 'Available' / 'Avail.'
    t = re.sub(r"(?i)^avail(able)?\.?\s*", "", t).strip()
    return t or "Now"


# ── Scraper ───────────────────────────────────────────────────────────────────
def scrape(headless: bool = True) -> list[dict]:
    """Return a list of floorplan dicts matching the existing data schema."""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        print(f"  → Navigating to {PROPERTY_URL}")
        page.goto(PROPERTY_URL, timeout=60_000, wait_until="domcontentloaded")

        # Wait for pricing sections to appear
        try:
            page.wait_for_selector('[data-tab-content-id], .pricingGridItem, .availabilityInfo', timeout=30_000)
        except PWTimeout:
            print("  ⚠ Timed out waiting for pricing grid — trying anyway…")

        # Give JS a moment to hydrate
        page.wait_for_timeout(3000)

        floorplans: list[dict] = []

        # ── Strategy 1: structured JSON-LD or window.__STATE__ ────────────────
        try:
            state_json = page.evaluate("""() => {
                // apartments.com sometimes embeds data as window.__STATE__
                if (window.__STATE__ && window.__STATE__.propertyDetails) {
                    return JSON.stringify(window.__STATE__.propertyDetails);
                }
                return null;
            }""")
            if state_json:
                print("  → Found window.__STATE__, parsing…")
                floorplans = _parse_state(json.loads(state_json))
        except Exception as e:
            print(f"  ⚠ window.__STATE__ strategy failed: {e}")

        # ── Strategy 2: DOM scraping ───────────────────────────────────────────
        if not floorplans:
            print("  → Falling back to DOM scraping…")
            floorplans = _scrape_dom(page)

        browser.close()

    print(f"  → Found {len(floorplans)} floorplans")
    return floorplans


def _parse_state(details: dict) -> list[dict]:
    """Parse apartments.com window.__STATE__ propertyDetails into floorplan list."""
    floorplans = []
    for fp_raw in details.get("floorPlans", []):
        fp_name = fp_raw.get("name", "").strip()
        beds    = _normalise_beds(fp_raw.get("beds", ""))
        baths   = _normalise_baths(fp_raw.get("baths", ""))
        sqft    = fp_raw.get("minSqFt") or 0
        deposit = fp_raw.get("deposit") or 0

        units = []
        for u in fp_raw.get("units", []):
            price = u.get("rentPrice") or u.get("maxRentPrice")
            if not price:
                continue
            units.append({
                "unit":         str(u.get("number", "")),
                "price":        int(price),
                "sqft":         int(u.get("sqFt") or sqft),
                "availability": parse_availability(u.get("availableDate", "Now")),
            })

        if not units:
            continue

        prices = [u["price"] for u in units]
        floorplans.append({
            "floorplan":      fp_name,
            "beds":           beds,
            "baths":          baths,
            "sqft":           int(sqft),
            "deposit":        int(deposit),
            "minPrice":       min(prices),
            "maxPrice":       max(prices),
            "availableUnits": len(units),
            "units":          units,
        })
    return floorplans


def _scrape_dom(page) -> list[dict]:
    """Scrape pricing grid from the rendered DOM."""
    floorplans = []

    # Each floorplan is a .pricingGridItem or [data-tab-content-id] section
    fp_sections = page.query_selector_all('.pricingGridItem, [class*="floorPlan"]')
    if not fp_sections:
        fp_sections = page.query_selector_all('[class*="placard"], [class*="pricing"]')

    for section in fp_sections:
        try:
            # Floorplan name
            name_el = section.query_selector('.modelName, [class*="planTitle"], h4')
            fp_name = name_el.inner_text().strip() if name_el else "Unknown"

            # Bed / bath
            bed_el  = section.query_selector('.bedsRange, [class*="bed"]')
            bath_el = section.query_selector('.bathsRange, [class*="bath"]')
            beds  = _normalise_beds(bed_el.inner_text().strip()  if bed_el  else "")
            baths = _normalise_baths(bath_el.inner_text().strip() if bath_el else "")

            # Sq ft
            sqft_el = section.query_selector('.sqftRange, [class*="sqft"]')
            sqft    = clean_sqft(sqft_el.inner_text()) if sqft_el else 0

            # Units
            units = []
            unit_rows = section.query_selector_all('.unitContainer, [class*="unit-"]')
            for row in unit_rows:
                # Unit number
                num_el  = row.query_selector('.unitColumn, [class*="unitNum"]')
                unit_no = num_el.inner_text().strip() if num_el else ""
                unit_no = re.sub(r"[^\w\-]", "", unit_no)

                # Price
                price_el = row.query_selector('.priceColumn, [class*="price"]')
                price    = clean_price(price_el.inner_text()) if price_el else None

                # Sqft per unit
                usqft_el = row.query_selector('.sqftColumn, [class*="sqft"]')
                u_sqft   = clean_sqft(usqft_el.inner_text()) if usqft_el else sqft

                # Availability
                avail_el = row.query_selector('.availableColumn, [class*="avail"]')
                avail    = parse_availability(avail_el.inner_text() if avail_el else "Now")

                if unit_no and price:
                    units.append({
                        "unit":         unit_no,
                        "price":        price,
                        "sqft":         u_sqft or sqft,
                        "availability": avail,
                    })

            if not units:
                continue

            prices = [u["price"] for u in units]
            floorplans.append({
                "floorplan":      fp_name,
                "beds":           beds,
                "baths":          baths,
                "sqft":           sqft or 0,
                "deposit":        750,           # default; not always shown
                "minPrice":       min(prices),
                "maxPrice":       max(prices),
                "availableUnits": len(units),
                "units":          units,
            })
        except Exception as e:
            print(f"    ⚠ Skipping section: {e}")
            continue

    return floorplans


def _normalise_beds(text: str) -> str:
    t = text.lower()
    if "studio" in t:               return "Studio"
    if "1" in t or "one" in t:      return "1 Bed"
    if "2" in t or "two" in t:      return "2 Bed"
    if "3" in t or "three" in t:    return "3 Bed"
    return text.title() or "Studio"


def _normalise_baths(text: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return "1 Bath"
    n = float(m.group(1))
    return f"{int(n) if n == int(n) else n} Bath{'s' if n > 1 else ''}"


# ── Data management ───────────────────────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    # Initialise fresh
    return {
        "property":  "Kurve",
        "url":       PROPERTY_URL,
        "address":   "2801 Sunset Pl, Los Angeles, CA 90005",
        "snapshots": [],
    }


def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  → Saved {DATA_FILE}")


def already_scraped_today(data: dict) -> bool:
    """Return True if the latest snapshot is from today (PST)."""
    snaps = data.get("snapshots", [])
    if not snaps:
        return False
    latest_ts = snaps[-1].get("timestamp", "")
    latest_date = latest_ts[:10]              # 'YYYY-MM-DD'
    today = datetime.now(PST).strftime("%Y-%m-%d")
    return latest_date == today


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Kurve Scraper — starting…")

    data = load_data()

    if already_scraped_today(data):
        print("  ✓ Already have today's data — skipping scrape.")
        return

    try:
        floorplans = scrape()
    except Exception as e:
        print(f"  ✗ Scrape failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not floorplans:
        print("  ✗ No floorplan data found — aborting to avoid overwriting with empty snapshot.", file=sys.stderr)
        sys.exit(1)

    snapshot = {
        "timestamp":  datetime.now(PST).strftime("%Y-%m-%dT00:00:00-07:00"),
        "floorplans": floorplans,
    }

    data["snapshots"].append(snapshot)

    # Keep last 90 snapshots (~3 months) to avoid the file growing unbounded
    data["snapshots"] = data["snapshots"][-90:]

    save_data(data)

    units_found = sum(fp["availableUnits"] for fp in floorplans)
    print(f"  ✓ Done — {len(floorplans)} floorplans, {units_found} units recorded.")


if __name__ == "__main__":
    main()
