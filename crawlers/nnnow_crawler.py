#!/usr/bin/env python3
"""
NNNow product page crawler.

NNNow (and similar custom React storefronts) don't expose a bulk
/products.json endpoint the way standard Shopify does, so this doesn't fit
the crawl -> match -> detail-scrape pipeline used for Shopify sites. It's a
direct URL-in, structured-data-out crawler: give it product page URLs, it
pulls the site's own embedded `window.DATA` JSON (sizes, stock, pricing,
images, composition, size chart) and writes a flat summary CSV + a full-detail
JSONL.

CLI mirrors the convention used elsewhere in this repo
(scrape_product_details.py): pass a path with --url-list to read URLs from a
file, or pass URLs directly as positional args.

Usage:
    python nnnow_crawler.py <url> [<url> ...] --output out.csv
    python nnnow_crawler.py urls.txt --url-list --output out.csv

Prints "PROGRESS i/n" to stdout after each URL so a calling UI (e.g. the
Streamlit control panel) can drive a real progress bar.
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

SUMMARY_FIELDS = [
    "title", "brand", "style_id", "mrp", "selling_price", "discount_pct",
    "in_stock", "num_sizes", "num_images", "has_size_chart", "source_url", "error",
]


def fetch(url: str, session: requests.Session, timeout: int = 20) -> str:
    resp = session.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _find_matching_brace(text: str, start: int) -> int:
    """Given the index of an opening '{', return the index of its matching '}'.
    Respects string literals and escape characters so braces inside strings
    (e.g. in a product description) don't throw off the count."""
    depth = 0
    i = start
    in_string = False
    string_char = ""
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == string_char:
                in_string = False
        else:
            if ch in ("'", '"'):
                in_string = True
                string_char = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def extract_js_object(html: str, var_name: str):
    """Find `window.<var_name> = { ... }` and JSON-parse it, using brace
    matching (not a single regex) so it survives nested braces/strings."""
    pattern = re.compile(r"window\.%s\s*=\s*" % re.escape(var_name))
    m = pattern.search(html)
    if not m:
        return None
    start = html.find("{", m.end())
    if start == -1:
        return None
    end = _find_matching_brace(html, start)
    if end == -1:
        return None
    try:
        return json.loads(html[start:end + 1])
    except json.JSONDecodeError:
        return None


def extract_json_ld(soup: BeautifulSoup):
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else data.get("@graph", [data])
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return None


def extract_meta_tags(soup: BeautifulSoup) -> dict:
    meta = {}
    mapping = {
        "og:title": "title", "og:image": "image", "og:url": "url",
        "og:description": "description",
        "product:price:amount": "price", "product:price:currency": "currency",
    }
    for tag in soup.find_all("meta"):
        prop = tag.get("property") or tag.get("name")
        if prop in mapping and tag.get("content"):
            meta[mapping[prop]] = tag["content"]
    return meta


def flatten_size_chart(chart_wrapper: dict):
    if not chart_wrapper:
        return None
    inner = (chart_wrapper.get("data") or {}).get("data") or {}
    measurements = inner.get("measurements") or {}

    def rows_to_dicts(rows):
        return [{item.get("attribute"): item.get("value") for item in row} for row in (rows or [])]

    return {
        "size_chart_id": chart_wrapper.get("id"),
        "measurements_cm": rows_to_dicts(measurements.get("cm")),
        "measurements_inch": rows_to_dicts(measurements.get("inch")),
        "chart_image": inner.get("primaryImage") or inner.get("imageUrl"),
        "chart_image_master": inner.get("masterImage"),
    }


def flatten_main_style(style: dict, source_url: str) -> dict:
    skus = style.get("skus", [])
    images = style.get("images", [])
    finer = style.get("finerDetails", {})
    color = style.get("colorDetails", {})
    about = style.get("about", {})
    sp_range = style.get("sellingPriceRange", {})
    mrp_range = style.get("mrpRange", {})

    sizes = [{
        "size": s.get("size"), "sku_id": s.get("skuId"), "mrp": s.get("mrp"),
        "price": s.get("price"), "discount_pct": s.get("discountInPercentage"),
        "in_stock": s.get("inStock"), "sellable_qty": s.get("sellableQuantity"),
    } for s in skus]

    other_colors = []
    for group in (style.get("colors", {}).get("colors", {}) or {}).values():
        for c in group:
            other_colors.append({
                "style_id": c.get("styleId"),
                "color": c.get("secondaryColor") or c.get("primaryColor"),
                "hex": c.get("hexCode"), "url": c.get("url"),
            })

    return {
        "source_url": source_url,
        "extraction": "window.DATA:mainStyle",
        "style_id": style.get("styleId"),
        "sap_style_id": style.get("sapStyleId"),
        "brand": style.get("brandName"),
        "title": style.get("name"),
        "description": style.get("story"),
        "gender": style.get("gender"),
        "category": style.get("pcmArticleType"),
        "url": style.get("url"),
        "in_stock": style.get("inStock"),
        "mrp": mrp_range.get("min", sizes[0]["mrp"] if sizes else None),
        "selling_price": sp_range.get("min", sizes[0]["price"] if sizes else None),
        "discount_pct": style.get("discountRange", {}).get("min"),
        "currency": "INR",
        "sizes": sizes,
        "images": [
            {"thumbnail": im.get("thumbnail"), "medium": im.get("medium"),
             "large": im.get("large"), "zoom": im.get("zoom")}
            for im in images
        ],
        "color": {"primary": color.get("primaryColor"), "secondary": color.get("secondaryColor"),
                   "hex": color.get("hexCode")},
        "other_colors": other_colors,
        "composition_and_care": finer.get("compositionAndCare", {}).get("list"),
        "specs": finer.get("specs", {}).get("list"),
        "manufacturer": style.get("manufacturerDetails"),
        "brand_story": about.get("story"),
        "promotions": [{"code": p.get("name"), "label": p.get("displayName")}
                        for p in style.get("promotions", [])],
        "average_rating": style.get("averageRating"),
        "ratings_count": style.get("ratingsCount"),
        "total_reviews": style.get("totalReviews"),
    }


def parse_product_page(html: str, url: str) -> dict:
    data = extract_js_object(html, "DATA")
    if data:
        try:
            main_style = data["ProductStore"]["PdpData"]["mainStyle"]
            result = flatten_main_style(main_style, url)
            size_chart = flatten_size_chart(data.get("ProductStore", {}).get("sizeChartData"))
            if size_chart:
                result["size_chart"] = size_chart
            return result
        except (KeyError, TypeError):
            pass  # fall through

    soup = BeautifulSoup(html, "html.parser")
    result = {"source_url": url, "extraction": []}

    json_ld = extract_json_ld(soup)
    if json_ld:
        result["extraction"].append("json_ld")
        offers = json_ld.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        result.update({
            "title": json_ld.get("name"),
            "brand": (json_ld.get("brand") or {}).get("name")
                     if isinstance(json_ld.get("brand"), dict) else json_ld.get("brand"),
            "description": json_ld.get("description"), "sku": json_ld.get("sku"),
            "image": json_ld.get("image"), "price": offers.get("price"),
            "currency": offers.get("priceCurrency"),
        })

    meta = extract_meta_tags(soup)
    if meta:
        result["extraction"].append("meta_tags")
        for k, v in meta.items():
            result.setdefault(k, v)

    return result


def to_summary_row(r: dict) -> dict:
    return {
        "title": r.get("title"), "brand": r.get("brand"), "style_id": r.get("style_id"),
        "mrp": r.get("mrp"), "selling_price": r.get("selling_price"),
        "discount_pct": r.get("discount_pct"), "in_stock": r.get("in_stock"),
        "num_sizes": len(r["sizes"]) if isinstance(r.get("sizes"), list) else "",
        "num_images": len(r["images"]) if isinstance(r.get("images"), list) else "",
        "has_size_chart": "size_chart" in r,
        "source_url": r.get("source_url"), "error": r.get("error", ""),
    }


def crawl(urls, delay: float, csv_path: str, jsonl_path: str):
    session = requests.Session()
    n = len(urls)

    with open(csv_path, "w", newline="", encoding="utf-8") as csvf, \
         open(jsonl_path, "w", encoding="utf-8") as jsonlf:
        writer = csv.DictWriter(csvf, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()

        for i, url in enumerate(urls):
            try:
                html = fetch(url, session)
                data = parse_product_page(html, url)
            except requests.RequestException as e:
                data = {"source_url": url, "error": str(e)}

            writer.writerow(to_summary_row(data))
            jsonlf.write(json.dumps(data, ensure_ascii=False) + "\n")
            csvf.flush()
            jsonlf.flush()

            print(f"PROGRESS {i + 1}/{n}  {url}", flush=True)

            if i < n - 1:
                time.sleep(delay)

    print(f"Done. {n} URLs -> {csv_path} , {jsonl_path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crawl NNNow (or similar custom-React storefront) product pages.")
    parser.add_argument("urls", nargs="+", help="Product URLs, or a single file path if --url-list is set")
    parser.add_argument("--url-list", action="store_true",
                         help="Treat the single positional arg as a path to a text file of URLs (one per line)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between requests")
    parser.add_argument("--output", required=True, help="Output CSV path (a sibling .jsonl is also written)")
    args = parser.parse_args()

    if args.url_list:
        if len(args.urls) != 1:
            print("--url-list expects exactly one path argument.", file=sys.stderr)
            sys.exit(1)
        url_list = [l.strip() for l in Path(args.urls[0]).read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        url_list = args.urls

    if not url_list:
        print("No URLs provided.", file=sys.stderr)
        sys.exit(1)

    csv_out = args.output
    jsonl_out = str(Path(csv_out).with_suffix(".jsonl"))
    Path(csv_out).parent.mkdir(parents=True, exist_ok=True)

    crawl(url_list, delay=args.delay, csv_path=csv_out, jsonl_path=jsonl_out)
