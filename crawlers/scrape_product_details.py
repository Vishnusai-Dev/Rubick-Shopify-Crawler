#!/usr/bin/env python3
"""
Product Detail Attribute Scraper (multi-site)
=========================================================
Pulls rich PDP attribute data (material, fit, pattern, dimensions,
manufacturer info, images, etc.) that's NOT available through Shopify's
/products.json REST API - these are custom metafields/theme content that
only exist in the rendered page HTML, so this scrapes live pages directly.

Site-specific parsing is dispatched via config/sites.yaml + site_parsers/
(see that package's docstring). To onboard a new site, see README.md ->
"Onboarding a New Website".

Usage - enrich an existing catalog CSV (from shopify_catalog_downloader.py):
    pip install -r requirements.txt
    python scrape_product_details.py ../data/catalog_downloads/celio_in.csv --base-url "https://celio.in"

Usage - scrape a plain list of product URLs instead:
    python scrape_product_details.py product_urls.txt --url-list --output details.csv

Output: a CSV keyed by "handle" with one column per attribute label found
(columns vary per product/site; missing attributes are left blank). Join
this back to your main catalog CSV on the "handle" column in Excel/pandas.
"""

import argparse
import csv
import json
import os
import random
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from site_parsers import get_parser_for_url

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

WORKERS = 4
TIMEOUT = 15
DELAY_MIN = 0.3
DELAY_MAX = 0.7
RETRY_TOTAL = 2

BLOCK_THRESHOLD = 3
COOLDOWN_SECONDS = 60

MAX_PASSES = 3
RETRY_PASS_DELAY = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
    )
}

STOP_EVENT = threading.Event()


def handle_sigint(signum, frame):
    if not STOP_EVENT.is_set():
        STOP_EVENT.set()
        print("\n\n  Ctrl+C received - stopping and saving what's been fetched so far...\n")
    else:
        print("\n  Force-quitting now.")
        os._exit(1)


signal.signal(signal.SIGINT, handle_sigint)


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def create_session():
    session = requests.Session()
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=RETRY_TOTAL, backoff_factor=1.0,
        status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


def extract_handle_from_url(url: str) -> str:
    m = re.search(r"/products/([^/?#]+)", url)
    return m.group(1) if m else url


def _walk_collect(obj, key, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                if isinstance(v, list):
                    found.extend(str(x) for x in v if x)
                elif v:
                    found.append(str(v))
            _walk_collect(v, key, found)
    elif isinstance(obj, list):
        for item in obj:
            _walk_collect(item, key, found)


def extract_images(soup: BeautifulSoup, page_url: str) -> list:
    """Pull product image URLs, preferring the Product-typed JSON-LD block
    (avoids picking up unrelated images like an Organization logo)."""
    images = []

    product_blocks = []
    other_blocks = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except Exception:
            continue
        block_type = data.get("@type")
        type_str = block_type if isinstance(block_type, str) else " ".join(block_type or [])
        if "Product" in type_str:
            product_blocks.append(data)
        else:
            other_blocks.append(data)

    for data in product_blocks:
        _walk_collect(data, "image", images)

    if not images:
        for data in other_blocks:
            _walk_collect(data, "image", images)

    if not images:
        for meta_name in [("property", "og:image"), ("name", "twitter:image")]:
            attr, val = meta_name
            for meta in soup.find_all("meta", attrs={attr: val}):
                content = meta.get("content")
                if content:
                    images.append(content)

    seen = set()
    normalized = []
    for img in images:
        full_url = urljoin(page_url, img)
        if full_url not in seen:
            seen.add(full_url)
            normalized.append(full_url)

    return normalized


def fetch_and_parse(session, url):
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    parser = get_parser_for_url(url)
    attrs = parser(soup)

    images = extract_images(soup, url)
    if images:
        attrs["main_image"] = images[0]
        attrs["all_images"] = "; ".join(images)
        attrs["image_count"] = len(images)

    return attrs


def scrape_all(urls: list) -> tuple:
    total = len(urls)
    print(f"Scraping product details for {total} URLs ({WORKERS} workers)...")

    session = create_session()
    latencies = []
    overall_start = time.time()

    breaker_lock = threading.Lock()
    consecutive_blocks = [0]
    cooldown_until = [0.0]

    def note_result(is_block):
        with breaker_lock:
            if is_block:
                consecutive_blocks[0] += 1
                if consecutive_blocks[0] >= BLOCK_THRESHOLD:
                    cooldown_until[0] = time.time() + COOLDOWN_SECONDS
                    consecutive_blocks[0] = 0
                    print(f"    !! repeated blocks/rate-limits - pausing {COOLDOWN_SECONDS}s...")
            else:
                consecutive_blocks[0] = 0

    def wait_if_cooling_down():
        wait = cooldown_until[0] - time.time()
        if wait > 0:
            time.sleep(wait)

    def worker(url):
        wait_if_cooling_down()
        handle = extract_handle_from_url(url)
        req_start = time.time()
        try:
            attrs = fetch_and_parse(session, url)
            latency = time.time() - req_start
            note_result(is_block=False)
            if not attrs:
                return handle, url, None, "no attributes found on page", latency
            return handle, url, attrs, None, latency
        except requests.exceptions.HTTPError as e:
            latency = time.time() - req_start
            status = e.response.status_code if e.response is not None else None
            note_result(is_block=status in (403, 429))
            return handle, url, None, f"HTTPError {status}", latency
        except Exception as e:
            latency = time.time() - req_start
            is_block = "429" in str(e) or "403" in str(e)
            note_result(is_block=is_block)
            reason = "RateLimited (429)" if is_block else f"{type(e).__name__}: {e}"
            return handle, url, None, reason, latency

    def run_pass(urls_to_try, pass_label):
        pass_results = []
        pass_failures = []
        pass_start = time.time()

        executor = ThreadPoolExecutor(max_workers=WORKERS)
        try:
            futures = {}
            for u in urls_to_try:
                if STOP_EVENT.is_set():
                    break
                futures[executor.submit(worker, u)] = u
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            done = 0
            for future in as_completed(futures):
                handle, url, attrs, error, latency = future.result()
                done += 1
                latencies.append(latency)
                if done % 25 == 0 or done == len(futures):
                    elapsed = time.time() - pass_start
                    rate = done / elapsed if elapsed > 0 else 0
                    remaining = (len(futures) - done) / rate if rate > 0 else 0
                    avg_latency = sum(latencies[-25:]) / len(latencies[-25:])
                    print(f"    [{pass_label}] {done}/{len(futures)} ({done/len(futures)*100:.1f}%) | "
                          f"elapsed {format_duration(elapsed)} | ~{rate:.1f}/s | "
                          f"avg latency {avg_latency:.2f}s | ETA {format_duration(remaining)}")

                if error:
                    pass_failures.append((handle, url, error))
                else:
                    row = {"handle": handle, "product_url": url}
                    row.update(attrs)
                    pass_results.append(row)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return pass_results, pass_failures

    all_results = []
    remaining_urls = urls
    final_failures = []

    for pass_num in range(1, MAX_PASSES + 1):
        if not remaining_urls or STOP_EVENT.is_set():
            break
        label = f"pass {pass_num}/{MAX_PASSES}"
        pass_results, pass_failures = run_pass(remaining_urls, label)
        all_results.extend(pass_results)

        if not pass_failures:
            remaining_urls = []
            break

        remaining_urls = [u for u in remaining_urls if extract_handle_from_url(u) in
                           {h for h, _, _ in pass_failures}]

        if STOP_EVENT.is_set():
            final_failures = pass_failures
            break
        elif pass_num < MAX_PASSES:
            print(f"    {len(remaining_urls)} still failing after {label}, "
                  f"retrying in {RETRY_PASS_DELAY}s...")
            time.sleep(RETRY_PASS_DELAY)
        else:
            final_failures = pass_failures

    elapsed = time.time() - overall_start
    print(f"\nDone in {format_duration(elapsed)}: {len(all_results)}/{total} succeeded, "
          f"{len(final_failures)} failed after {MAX_PASSES} passes.")
    if final_failures:
        reason_counts = {}
        for _, _, reason in final_failures:
            key = reason.split(":")[0]
            reason_counts[key] = reason_counts.get(key, 0) + 1
        print("Failure breakdown:")
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  - {reason}: {count}")

    return all_results, final_failures


def save_results(results: list, output_path: str):
    if not results:
        print("No results to save.")
        return

    fieldnames = ["handle", "product_url"]
    seen = set(fieldnames)
    for row in results:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"Saved: {output_path} ({len(results)} rows, {len(fieldnames)} columns)")


def main():
    parser = argparse.ArgumentParser(description="Scrape rich product-detail attributes from PDPs")
    parser.add_argument("input", help="Catalog CSV (from shopify_catalog_downloader.py) or a plain text file of URLs")
    parser.add_argument("--url-list", action="store_true", help="Treat input as a plain text file of URLs, one per line")
    parser.add_argument("--base-url", default=None, help="Store base URL, used with a catalog CSV to build product URLs from 'handle'")
    parser.add_argument("--output", default=None, help="Output CSV path")
    args = parser.parse_args()

    if args.url_list:
        with open(args.input, "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip()]
    else:
        if not args.base_url:
            print("Error: --base-url is required when reading a catalog CSV (to build product URLs from handles).")
            sys.exit(1)
        import pandas as pd
        df = pd.read_csv(args.input, dtype=str)
        if "handle" not in df.columns:
            print("Error: input CSV has no 'handle' column.")
            sys.exit(1)
        handles = df["handle"].dropna().unique().tolist()
        base = args.base_url.rstrip("/")
        urls = [f"{base}/products/{h}" for h in handles]

    if not urls:
        print("No URLs to scrape.")
        sys.exit(1)

    output_path = args.output or "product_details.csv"
    results, failures = scrape_all(urls)
    save_results(results, output_path)

    if failures:
        failures_path = os.path.splitext(output_path)[0] + "_unresolved.csv"
        with open(failures_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["handle", "url", "reason"])
            writer.writerows(failures)
        print(f"Unresolved URLs saved to: {failures_path}")


if __name__ == "__main__":
    main()
