import os
import re
import json
import time
import random
import asyncio
from datetime import datetime
import pandas as pd
import requests
import cloudscraper
import gspread
from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PWTimeout

# ==========================================
# 1. GOOGLE SHEETS CONFIG
# ==========================================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1X6eCSHhDB1VEJZ0dpRkk9ts4q9g4FiEX0RKJu6D1tkk/edit"
CREDENTIALS_FILE = "credentials.json"

def get_workbook():
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    return gc.open_by_url(SHEET_URL)

def read_config():
    ws = get_workbook().worksheet("Config")
    data = ws.get_all_records(default_blank="")
    return pd.DataFrame(data).astype(object)

def write_report(df):
    ws = get_workbook().worksheet("Report")
    df = df.fillna("")
    ws.clear()
    ws.update([df.columns.tolist()] + df.values.tolist())

# ==========================================
# 2. COMPETITOR CONFIGURATION
# ==========================================
# method:
#   css          - extract text from a CSS selector
#   jsonld       - read price from JSON-LD <script> offers.price
#   wc_variation - match WooCommerce data-product_variations by variant attributes
#   camoufox     - headless Firefox with anti-bot fingerprinting (alta.ge)

COMPETITOR_CONFIG = {
    "ispot": {
        "method": "css",
        "selector": "span.font-semibold span.price-value",
    },
    "gstore": {
        "method": "css",
        "selector": "p.price .woocommerce-Price-amount",
    },
    "usmobi": {
        "method": "css",
        "selector": "#pirveli2 h2",  # Category A price
    },
    "alta": {
        "method": "camoufox",
    },
    "myphones": {
        "method": "wc_variation",
        # myphones_variant column: e.g. "128GB|Black"
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9"
}

# ==========================================
# 3. CAMOUFOX (alta.ge)
# ==========================================
_ALTA_CACHE = {}

CF_CHALLENGE_STRINGS = ["just a moment", "einen moment", "un momento", "attention required", "checking your"]

async def _get_alta_price(browser, url):
    for attempt in range(3):
        page = await browser.new_page()
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except PWTimeout:
                await asyncio.sleep(3)
                continue
            # Wait up to 20s for CF challenge to resolve; abort if it doesn't
            for _ in range(4):
                await asyncio.sleep(5)
                title = await page.title()
                if not any(x in title.lower() for x in CF_CHALLENGE_STRINGS):
                    break
            else:
                return "CF Blocked"
            if any(x in str(title) for x in ["502", "503"]):
                await asyncio.sleep(3)
                continue
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    for node in data.get("@graph", [data]):
                        if node.get("@type") == "Product":
                            offers = node.get("offers", {})
                            price = offers.get("price")
                            if price is not None:
                                oos = "OutOfStock" in offers.get("availability", "")
                                return fmt(float(price), oos)
                except Exception:
                    continue
            return "Element Not Found"
        finally:
            await page.close()
    return "Failed"

async def _fetch_alta_async(urls):
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=True) as browser:
        for url in urls:
            _ALTA_CACHE[url] = await _get_alta_price(browser, url)
            print(f"  alta: {_ALTA_CACHE[url]}")

def prefetch_alta(config):
    if "alta_URL" not in config.columns:
        return
    urls = [
        str(row.get("alta_URL", "")).strip()
        for _, row in config.iterrows()
        if str(row.get("alta_URL", "")).strip().startswith("http")
    ]
    if not urls:
        return
    print(f"Pre-fetching {len(urls)} alta.ge URLs via Camoufox...")
    asyncio.run(_fetch_alta_async(urls))

# ==========================================
# 4. EXTRACTION CORE
# ==========================================
def fetch_page(url, use_cloudscraper=False):
    if use_cloudscraper:
        scraper = cloudscraper.create_scraper()
        return scraper.get(url.strip(), timeout=15)
    return requests.get(url.strip(), headers=HEADERS, timeout=10)


def extract_number(text):
    clean = ''.join(c for c in text if c.isdigit() or c in ['.', ','])
    clean = clean.replace(',', '')
    return float(clean) if clean else None


def is_out_of_stock(soup):
    if soup.find(string=lambda t: t and 'მარაგი ამოიწურა' in t):
        return True
    if soup.select_one('.out-of-stock, .stock.out-of-stock'):
        return True
    return False


def fmt(price, oos):
    return f"{price} (OOS)" if oos else price


def extract_price(url, competitor, variant_filter=None):
    if not url or not str(url).strip().startswith("http"):
        return ""

    config = COMPETITOR_CONFIG.get(competitor, {})
    method = config.get("method", "css")

    # --- Camoufox (alta.ge) — results pre-fetched before main loop ---
    if method == "camoufox":
        return _ALTA_CACHE.get(str(url).strip(), "Not Fetched")

    try:
        response = fetch_page(url, use_cloudscraper=config.get("use_cloudscraper", False))

        if response.status_code != 200:
            return "Blocked (403)" if response.status_code == 403 else f"Error {response.status_code}"

        soup = BeautifulSoup(response.text, "html.parser")
        oos = is_out_of_stock(soup)

        # --- CSS selector ---
        if method == "css":
            el = soup.select_one(config["selector"])
            if not el:
                return "OOS" if oos else "Element Not Found"
            val = extract_number(el.get_text(strip=True))
            return fmt(val, oos) if val is not None else "Format Error"

        # --- JSON-LD ---
        elif method == "jsonld":
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    nodes = data.get("@graph", [data])
                    for node in nodes:
                        if node.get("@type") == "Product":
                            offers = node.get("offers", {})
                            price = offers.get("price")
                            if price is not None:
                                availability = offers.get("availability", "")
                                oos = "OutOfStock" in availability
                                return fmt(float(price), oos)
                except Exception:
                    continue
            return "Element Not Found"

        # --- WooCommerce variations (myphones.ge) ---
        elif method == "wc_variation":
            form = soup.find("form", class_="variations_form")
            if not form:
                return "Element Not Found"
            import html as htmllib
            variations = json.loads(htmllib.unescape(form.get("data-product_variations", "[]")))

            if not variant_filter:
                prices = [v["display_price"] for v in variations if v.get("display_price")]
                return fmt(float(min(prices)), oos) if prices else "No Variants"

            wanted = [p.strip().lower() for p in variant_filter.split("|") if p.strip()]
            for v in variations:
                attr_values = [str(val).lower() for val in v.get("attributes", {}).values()]
                if all(any(w in av for av in attr_values) for w in wanted):
                    in_stock = v.get("is_in_stock", True)
                    return fmt(float(v["display_price"]), not in_stock)

            # Fallback: bidirectional match
            for v in variations:
                attr_values = [str(val).lower() for val in v.get("attributes", {}).values()]
                if all(any(w in av or av in w for av in attr_values) for w in wanted):
                    in_stock = v.get("is_in_stock", True)
                    return fmt(float(v["display_price"]), not in_stock)

            return "Variant Not Found"

    except Exception as e:
        return f"Failed: {str(e)[:30]}"


# ==========================================
# 5. EXECUTION PIPELINE
# ==========================================
def main():
    print("Reading config from Google Sheet...")
    config = read_config()
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    competitors = [c for c in COMPETITOR_CONFIG if f"{c}_URL" in config.columns]

    print(f"Competitors: {', '.join(competitors)}")
    print(f"Products: {len(config)}\n" + "=" * 45)

    prefetch_alta(config)

    # Build report rows as we scan
    report_rows = []

    for idx, row in config.iterrows():
        print(f"Scanning: {row['Product_Name']}")
        report_row = {"Product": row["Product_Name"]}

        if "My_Price" in config.columns and str(row.get("My_Price", "")).strip():
            report_row["My Price"] = row["My_Price"]

        for comp in competitors:
            url = row.get(f"{comp}_URL", "")
            variant = row.get(f"{comp}_variant", "")
            price = extract_price(url, comp, variant_filter=variant if str(variant).strip() else None)
            report_row[comp] = price
            if price != "" and comp != "alta":
                print(f"  {comp}: {price}")

        report_row["Last Updated"] = current_time
        report_rows.append(report_row)
        time.sleep(random.uniform(1.5, 3.0))

    report_df = pd.DataFrame(report_rows)

    print("\nWriting report to Google Sheet...")
    write_report(report_df)

    print("\n" + "=" * 45)
    print(report_df.to_string(index=False))
    print("=" * 45 + "\nDone.")

if __name__ == "__main__":
    main()
