#!/usr/bin/env python3
"""
Product Page Structure Inspector
==================================
Metafield-style attributes (Primary Material, Fit, Pattern, Closure Type,
dimensions, etc.) shown on a PDP are usually NOT in Shopify's public
/products.json or /products/<handle>.json - those only cover core fields
(title, price, sku, variants, images...). Custom attributes like these are
almost always Shopify metafields, which live in the page's rendered HTML
or an embedded data blob, not the REST product API.

This script checks a single product URL for the most common ways that
data gets embedded, and prints what it finds so we know exactly which
approach to build the full scraper around:
  1. Framework-embedded JSON (Next.js __NEXT_DATA__, Nuxt, __INITIAL_STATE__, etc.)
  2. JSON-LD structured data (<script type="application/ld+json">)
  3. The visible "Product Details" section, extracted as plain text

Usage:
    pip install requests beautifulsoup4 lxml
    python inspect_product_page.py "https://celio.in/products/men-green-poly-cotton-solid-t-shirts-megaufre-green-mid"
"""

import json
import re
import sys

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
    )
}


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def check_embedded_json(soup: BeautifulSoup):
    print("=" * 70)
    print("1. Checking for framework-embedded JSON blobs...")
    print("=" * 70)

    found_any = False

    # Next.js
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data:
        found_any = True
        print("\n[FOUND] __NEXT_DATA__ (Next.js) script tag.")
        try:
            data = json.loads(next_data.string)
            print("  Top-level keys:", list(data.keys()))
            # Try to locate anything product-related
            as_text = json.dumps(data)
            if "Primary Material" in as_text or "material" in as_text.lower():
                print("  -> Looks like it contains material/attribute data. Good sign.")
            snippet = as_text[:1500]
            print(f"  First 1500 chars of the JSON:\n  {snippet}\n")
        except Exception as e:
            print(f"  Could not parse as JSON: {e}")

    # Nuxt
    nuxt_data = soup.find("script", id="__NUXT_DATA__") or soup.find(string=re.compile(r"window\.__NUXT__"))
    if nuxt_data:
        found_any = True
        print("\n[FOUND] Nuxt-style embedded data.")

    # Generic window.__INITIAL_STATE__ / __APOLLO_STATE__ / etc.
    for script in soup.find_all("script"):
        text = script.string or ""
        for marker in ["__INITIAL_STATE__", "__APOLLO_STATE__", "__PRELOADED_STATE__", "window.__DATA__"]:
            if marker in text:
                found_any = True
                print(f"\n[FOUND] Script containing '{marker}'.")
                idx = text.find(marker)
                print(f"  Context: ...{text[max(0,idx-50):idx+300]}...\n")

    if not found_any:
        print("\n  No common framework JSON blobs found. This site likely renders "
              "everything server-side as plain HTML - we'll need to scrape the "
              "visible 'Product Details' section directly (see section 3 below).")


def check_json_ld(soup: BeautifulSoup):
    print("\n" + "=" * 70)
    print("2. Checking for JSON-LD structured data...")
    print("=" * 70)

    ld_scripts = soup.find_all("script", type="application/ld+json")
    if not ld_scripts:
        print("\n  No JSON-LD script tags found.")
        return

    for i, script in enumerate(ld_scripts, 1):
        try:
            data = json.loads(script.string)
            print(f"\n[JSON-LD block {i}] type: {data.get('@type', 'unknown')}")
            print(f"  Keys: {list(data.keys())}")
            if data.get("@type") == "Product":
                print(f"  Full content:\n{json.dumps(data, indent=2)[:1500]}")
        except Exception as e:
            print(f"  Block {i}: could not parse ({e})")


def check_visible_product_details(soup: BeautifulSoup, search_terms=None):
    print("\n" + "=" * 70)
    print("3. Extracting visible product-attribute sections as plain text...")
    print("=" * 70)

    if not search_terms:
        search_terms = ["Product Details", "Manufacturer Details", "Description",
                         "Manufacture Details", "Specifications"]

    any_found = False
    for term in search_terms:
        pattern = re.compile(re.escape(term).replace(r"\ ", r"\s+"), re.IGNORECASE)
        heading = soup.find(string=pattern)
        if not heading:
            print(f"\n  [{term}] - not found in static HTML.")
            continue

        any_found = True
        heading_el = heading.find_parent()
        print(f"\n  [{term}] - FOUND in a <{heading_el.name}> tag.")

        container = heading_el
        for _ in range(5):
            if container.parent:
                container = container.parent
            text_len = len(container.get_text(strip=True))
            if text_len > 200:
                break

        lines = [line.strip() for line in container.get_text("\n").split("\n") if line.strip()]
        print(f"  Raw text lines from that container (first 30):")
        for line in lines[:30]:
            print(f"    {line!r}")

        print(f"\n  Raw HTML snippet around the heading (for building CSS selectors):")
        print(f"  {str(container)[:1500]}")

    if not any_found:
        print("\n  None of the searched headings were found in the static HTML. "
              "This likely means the content loads dynamically via JavaScript "
              "AFTER page load (client-side fetch) - a plain requests.get() "
              "won't see it, and we'd need a headless browser (Selenium/Playwright) instead.")


def main():
    if len(sys.argv) < 2:
        url = input("Paste a product page URL: ").strip()
    else:
        url = sys.argv[1]

    search_terms = None
    if len(sys.argv) > 2:
        search_terms = [t.strip() for t in sys.argv[2].split(",") if t.strip()]

    print(f"Fetching: {url}\n")
    html = fetch_html(url)
    soup = BeautifulSoup(html, "lxml")

    check_embedded_json(soup)
    check_json_ld(soup)
    check_visible_product_details(soup, search_terms)

    print("\n" + "=" * 70)
    print("Done. Copy the output above and share it back - it tells us exactly "
          "which extraction method will work reliably for this site.")
    print("=" * 70)


if __name__ == "__main__":
    main()
