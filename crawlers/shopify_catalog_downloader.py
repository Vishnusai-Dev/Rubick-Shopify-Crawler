#!/usr/bin/env python3
"""
Shopify Catalog Downloader
---------------------------
Pulls full product catalogs from public Shopify /products.json endpoints
for one or more stores, and saves:
  - raw JSON per store (all products)
  - a flattened CSV per store (one row per variant)

Usage (interactive - just run it and paste URLs when asked):
    pip install requests
    python shopify_catalog_downloader.py

Usage (non-interactive, one or more URLs as arguments):
    python shopify_catalog_downloader.py "https://myraymond.com/" "https://www.jackjones.in/"

Skip barcode backfill (much faster, bulk data only, no per-product requests):
    python shopify_catalog_downloader.py "https://myraymond.com/" --no-barcode

Force barcode backfill on (overrides the BACKFILL_BARCODES default below):
    python shopify_catalog_downloader.py "https://myraymond.com/" --barcode
"""

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

# Global stop signal: set on Ctrl+C so in-flight loops can wind down cleanly
# and save partial progress, instead of the default (which waits for every
# already-queued task in the thread pool to finish before exiting).
STOP_EVENT = threading.Event()


def handle_sigint(signum, frame):
    if not STOP_EVENT.is_set():
        STOP_EVENT.set()
        print("\n\n  Ctrl+C received - stopping, cancelling pending requests, "
              "and saving what's been fetched so far...")
        print("  (press Ctrl+C again to force-quit immediately without saving)\n")
    else:
        print("\n  Force-quitting now.")
        os._exit(1)


signal.signal(signal.SIGINT, handle_sigint)

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

# Anchored to this script's own location, not the caller's current directory -
# this matters because Streamlit runs this as a subprocess from a different
# working directory than a direct CLI run would, and both need to agree on
# where output lands (the app looks for it at repo_root/data/catalog_downloads).
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "catalog_downloads")
PAGE_LIMIT = 250          # Shopify's max page size
REQUEST_DELAY = 1.0       # seconds between requests, be polite / avoid rate-limits
MAX_PAGES = 200           # safety cap (200 * 250 = 50,000 products per store)
TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CatalogResearchBot/1.0; +https://example.com)"
}

# The bulk /products.json listing often omits "barcode" even when it exists -
# it's only reliably present on the single-product endpoint. Set this to
# True to do a second pass fetching every product individually to backfill
# real barcode values (slower: one request per product instead of per 250).
BACKFILL_BARCODES = True
BARCODE_WORKERS = 10             # concurrent requests during backfill
BACKFILL_DELAY_MIN = 0.05        # randomized stagger between request submissions (seconds)
BACKFILL_DELAY_MAX = 0.20
BARCODE_TIMEOUT = 12             # shorter timeout so hung requests fail fast instead of eating minutes
BARCODE_RETRY_TOTAL = 2          # fewer retries per request - fail fast, circuit breaker handles the rest
BARCODE_RETRY_BACKOFF = 1.0

# Circuit breaker: if we start seeing block/rate-limit signals, pause the
# whole crawl instead of hammering through them.
BLOCK_THRESHOLD = 5              # consecutive 403/429s that trigger a pause
COOLDOWN_SECONDS = 45            # how long to pause when tripped

# Multi-pass retry: instead of permanently dropping products that fail once,
# retry just the failed ones in subsequent passes (transient errors resolve
# on retry far more often than they don't).
BACKFILL_MAX_PASSES = 3
RETRY_PASS_DELAY = 20            # seconds to wait before starting a retry pass

# ----------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def normalize_base_url(url: str) -> str:
    """Return scheme://host with no path, no query, no trailing junk."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def create_session(retry_total=3, backoff_factor=1.5):
    session = requests.Session()
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=retry_total,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


# One shared session for discovery requests, so retries/backoff apply here too -
# previously these used plain requests.get() with zero retry logic, unlike the
# barcode backfill phase which already had this.
_discovery_session = create_session(retry_total=5, backoff_factor=2.0)


def fetch_page_via_param(base_url: str, page: int, collection_handle: str = None) -> list:
    """Try classic ?page=N&limit=250 pagination."""
    if collection_handle:
        endpoint = urljoin(base_url, f"/collections/{collection_handle}/products.json")
    else:
        endpoint = urljoin(base_url, "/products.json")

    resp = _discovery_session.get(
        endpoint,
        params={"limit": PAGE_LIMIT, "page": page},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("products", [])


def fetch_via_cursor(base_url: str, collection_handle: str = None) -> list:
    """Fallback: cursor-based pagination using the Link header (page_info)."""
    if collection_handle:
        endpoint = urljoin(base_url, f"/collections/{collection_handle}/products.json")
    else:
        endpoint = urljoin(base_url, "/products.json")

    all_products = []
    params = {"limit": PAGE_LIMIT}
    url = endpoint

    for _ in range(MAX_PAGES):
        resp = _discovery_session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        products = resp.json().get("products", [])
        if not products:
            break
        all_products.extend(products)

        link_header = resp.headers.get("Link", "")
        next_match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        if not next_match:
            break
        url = next_match.group(1)
        params = {}  # next URL already has all query params embedded
        time.sleep(REQUEST_DELAY)

    return all_products


def fetch_single_product(session: requests.Session, base_url: str, handle: str) -> dict:
    """Fetch one product's full detail via /products/<handle>.json - this endpoint
    reliably includes barcode, unlike the bulk /products.json listing."""
    endpoint = urljoin(base_url, f"/products/{handle}.json")
    resp = session.get(endpoint, timeout=BARCODE_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("product", {})


def backfill_barcodes(base_url: str, products: list, store_name: str) -> list:
    """Fetch each product individually and merge real barcode values back
    into the bulk-discovered product list (matched by variant id).
    Makes multiple passes, retrying only products that failed each time,
    so transient errors don't permanently drop coverage."""
    total = len(products)
    print(f"  backfilling barcodes for {total} products (individual requests, {BARCODE_WORKERS} workers)...")

    session = create_session(retry_total=BARCODE_RETRY_TOTAL, backoff_factor=BARCODE_RETRY_BACKOFF)
    latencies = []  # seconds per request, for diagnostics
    start_time = time.time()

    # --- circuit breaker state, shared across worker threads and passes ---
    breaker_lock = threading.Lock()
    consecutive_blocks = [0]
    cooldown_until = [0.0]

    def note_result(is_block: bool):
        with breaker_lock:
            if is_block:
                consecutive_blocks[0] += 1
                if consecutive_blocks[0] >= BLOCK_THRESHOLD:
                    cooldown_until[0] = time.time() + COOLDOWN_SECONDS
                    consecutive_blocks[0] = 0
                    print(f"    !! seeing repeated blocks/rate-limits - pausing all workers for "
                          f"{COOLDOWN_SECONDS}s to cool down...")
            else:
                consecutive_blocks[0] = 0

    def wait_if_cooling_down():
        wait = cooldown_until[0] - time.time()
        if wait > 0:
            time.sleep(wait)

    def worker(product):
        wait_if_cooling_down()
        handle = product.get("handle")
        if not handle:
            return product, None, "no handle", 0.0
        req_start = time.time()
        try:
            detail = fetch_single_product(session, base_url, handle)
            latency = time.time() - req_start
            if not detail or not detail.get("variants"):
                note_result(is_block=False)
                return product, None, "empty/unexpected response body", latency
            note_result(is_block=False)
            return product, detail, None, latency
        except requests.exceptions.HTTPError as e:
            latency = time.time() - req_start
            status = e.response.status_code if e.response is not None else None
            is_block = status in (403, 429)
            note_result(is_block=is_block)
            return product, None, f"HTTPError {status}", latency
        except Exception as e:
            latency = time.time() - req_start
            note_result(is_block=False)
            return product, None, f"{type(e).__name__}: {e}", latency

    def run_pass(products_to_try, pass_label):
        """One pass over a list of products. Returns list of (handle, reason) still-unresolved."""
        pass_failures = []
        done = 0
        pass_total = len(products_to_try)
        pass_start = time.time()

        executor = ThreadPoolExecutor(max_workers=BARCODE_WORKERS)
        try:
            futures = {}
            submitted_handles = set()
            for p in products_to_try:
                if STOP_EVENT.is_set():
                    break
                futures[executor.submit(worker, p)] = p
                submitted_handles.add(p.get("handle"))
                time.sleep(random.uniform(BACKFILL_DELAY_MIN, BACKFILL_DELAY_MAX))

            for future in as_completed(futures):
                product, detail, error, latency = future.result()
                done += 1
                latencies.append(latency)
                if done % 50 == 0 or done == len(futures):
                    elapsed = time.time() - pass_start
                    rate = done / elapsed if elapsed > 0 else 0
                    remaining = (len(futures) - done) / rate if rate > 0 else 0
                    pct = done / len(futures) * 100
                    recent = latencies[-50:]
                    avg_latency = sum(recent) / len(recent) if recent else 0
                    print(
                        f"    [{pass_label}] {done}/{len(futures)} ({pct:.1f}%) | "
                        f"elapsed {format_duration(elapsed)} | "
                        f"~{rate:.1f}/s | "
                        f"avg latency {avg_latency:.2f}s | "
                        f"ETA {format_duration(remaining)}"
                    )

                if error:
                    pass_failures.append((product.get("handle", "?"), error))
                    continue

                barcode_by_variant_id = {
                    v.get("id"): v.get("barcode") for v in detail.get("variants", [])
                }
                got_any_barcode = False
                for v in product.get("variants", []):
                    bc = barcode_by_variant_id.get(v.get("id"))
                    if bc:
                        v["barcode"] = bc
                        got_any_barcode = True
                if not got_any_barcode:
                    pass_failures.append((product.get("handle", "?"), "fetched OK but no barcode values present"))

                if STOP_EVENT.is_set():
                    break
        finally:
            # cancel_futures=True (Python 3.9+) drops any not-yet-started tasks
            # immediately instead of waiting for the whole queue to drain.
            executor.shutdown(wait=False, cancel_futures=True)

        # Anything never submitted (stopped early) or never completed counts as unresolved
        unsubmitted = [p for p in products_to_try if p.get("handle") not in submitted_handles]
        for p in unsubmitted:
            pass_failures.append((p.get("handle", "?"), "skipped - interrupted by user"))

        return pass_failures



    remaining_products = products
    all_final_failures = []

    for pass_num in range(1, BACKFILL_MAX_PASSES + 1):
        if not remaining_products:
            break
        label = f"pass {pass_num}/{BACKFILL_MAX_PASSES}"
        pass_failures = run_pass(remaining_products, label)

        if not pass_failures:
            remaining_products = []
            break

        failed_handles = {h for h, _ in pass_failures}
        remaining_products = [p for p in remaining_products if p.get("handle") in failed_handles]

        if STOP_EVENT.is_set():
            all_final_failures = pass_failures
            break
        elif pass_num < BACKFILL_MAX_PASSES:
            print(f"    {len(remaining_products)} products still missing barcodes after {label}, "
                  f"retrying in {RETRY_PASS_DELAY}s...")
            time.sleep(RETRY_PASS_DELAY)
        else:
            all_final_failures = pass_failures

    success_count = total - len(all_final_failures)
    backfill_elapsed = time.time() - start_time
    avg_latency_overall = sum(latencies) / len(latencies) if latencies else 0

    print(f"  barcode backfill done in {format_duration(backfill_elapsed)}: "
          f"{success_count}/{total} products got at least one barcode. "
          f"(avg request latency: {avg_latency_overall:.2f}s)")

    if all_final_failures:
        # Breakdown by reason category
        reason_counts = {}
        for _, reason in all_final_failures:
            key = reason.split(":")[0]  # e.g. "HTTPError 403", "Timeout", "ConnectionError"
            reason_counts[key] = reason_counts.get(key, 0) + 1
        print(f"  {len(all_final_failures)} products unresolved after {BACKFILL_MAX_PASSES} passes. Breakdown:")
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"    - {reason}: {count}")

        # Save full failure list so these can be targeted/retried later without redoing the whole crawl
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        failures_path = os.path.join(OUTPUT_DIR, f"{store_name}_unresolved_barcodes.csv")
        with open(failures_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["handle", "reason"])
            writer.writerows(all_final_failures)
        print(f"  Unresolved product list saved to: {failures_path}")

    stats = {
        "barcode_success": success_count,
        "barcode_failed": len(all_final_failures),
        "backfill_seconds": round(backfill_elapsed, 1),
        "avg_request_latency": round(avg_latency_overall, 2),
    }
    return products, stats


def download_store_catalog(name: str, url: str) -> tuple:
    """Download all products for one store, trying page-param first, then cursor.
    Returns (products, stats) where stats has timing/count info for the summary report."""
    base_url = normalize_base_url(url)

    # If the given URL points at a specific collection, capture its handle
    collection_handle = None
    m = re.search(r"/collections/([^/?#]+)", url)
    if m and m.group(1) not in ("all", "frontpage"):
        collection_handle = m.group(1)

    print(f"\n[{name}] base: {base_url}" + (f" (collection: {collection_handle})" if collection_handle else ""))

    discovery_start = time.time()
    all_products = []
    seen_ids = set()

    # --- Attempt 1: classic page param ---
    page = 1
    used_page_param = False
    while page <= MAX_PAGES:
        if STOP_EVENT.is_set():
            print("  discovery stopped by user.")
            break
        try:
            products = fetch_page_via_param(base_url, page, collection_handle)
        except requests.exceptions.RequestException as e:
            print(f"  page {page}: request failed ({e}), stopping page-param attempt")
            break

        if not products:
            break

        used_page_param = True
        new = [p for p in products if p["id"] not in seen_ids]
        for p in new:
            seen_ids.add(p["id"])
        all_products.extend(new)
        elapsed = time.time() - discovery_start
        print(f"  page {page}: +{len(new)} products (total {len(all_products)}) | elapsed {format_duration(elapsed)}")

        if len(products) < PAGE_LIMIT:
            break  # last page
        page += 1
        time.sleep(REQUEST_DELAY)

    # --- Attempt 2: cursor-based fallback if page param yielded nothing ---
    if not used_page_param:
        print("  page-param pagination returned nothing, trying cursor-based (Link header)...")
        try:
            all_products = fetch_via_cursor(base_url, collection_handle)
            print(f"  cursor method: total {len(all_products)} products")
        except requests.exceptions.RequestException as e:
            print(f"  cursor method also failed: {e}")

    discovery_elapsed = time.time() - discovery_start
    print(f"  discovery done in {format_duration(discovery_elapsed)}: {len(all_products)} products found.")

    stats = {
        "products_found": len(all_products),
        "discovery_seconds": round(discovery_elapsed, 1),
        "barcode_success": None,
        "barcode_failed": None,
        "backfill_seconds": None,
    }

    if BACKFILL_BARCODES and all_products:
        all_products, backfill_stats = backfill_barcodes(base_url, all_products, name)
        stats.update(backfill_stats)

    return all_products, stats


def flatten_products_to_rows(products: list) -> list:
    """One row per variant, with key product + variant fields."""
    rows = []
    for p in products:
        base = {
            "product_id": p.get("id"),
            "title": p.get("title"),
            "handle": p.get("handle"),
            "vendor": p.get("vendor"),
            "product_type": p.get("product_type"),
            "tags": ", ".join(p.get("tags", [])) if isinstance(p.get("tags"), list) else p.get("tags"),
            "published_at": p.get("published_at"),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
            "image_src": (p.get("images") or [{}])[0].get("src") if p.get("images") else None,
        }
        variants = p.get("variants") or [{}]
        for v in variants:
            row = dict(base)
            row.update({
                "variant_id": v.get("id"),
                "variant_title": v.get("title"),
                "sku": v.get("sku"),
                "barcode": v.get("barcode"),
                "price": v.get("price"),
                "compare_at_price": v.get("compare_at_price"),
                "available": v.get("available"),
                "inventory_quantity": v.get("inventory_quantity"),
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
            })
            rows.append(row)
    return rows


def save_outputs(name: str, products: list):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    json_path = os.path.join(OUTPUT_DIR, f"{name}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

    rows = flatten_products_to_rows(products)
    csv_path = os.path.join(OUTPUT_DIR, f"{name}.csv")
    if rows:
        fieldnames = list(rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    print(f"  saved: {json_path} ({len(products)} products), {csv_path} ({len(rows)} variant rows)")


def derive_store_name(url: str) -> str:
    """Turn a URL into a filesystem-safe short name, e.g. https://pa.myraymond.com/ -> pa_myraymond_com"""
    netloc = urlparse(url).netloc or url
    netloc = netloc.replace("www.", "")
    name = re.sub(r"[^a-zA-Z0-9]+", "_", netloc).strip("_").lower()
    return name or "store"


def collect_urls_from_args_or_input() -> tuple:
    """
    Priority:
      1. URLs passed as command-line arguments (plus optional --no-barcode / --barcode flags).
      2. Otherwise, prompt interactively - paste one URL per line, blank line to finish,
         then ask whether to fetch barcodes.

    Returns (urls, backfill_barcodes: bool)
    """
    if len(sys.argv) > 1:
        args = sys.argv[1:]
        backfill = BACKFILL_BARCODES
        if "--no-barcode" in args:
            backfill = False
            args = [a for a in args if a != "--no-barcode"]
        elif "--barcode" in args:
            backfill = True
            args = [a for a in args if a != "--barcode"]
        urls = [u.strip() for u in args if u.strip()]
        return urls, backfill

    print("Paste one Shopify store/collection URL per line.")
    print("Press Enter on an empty line when you're done.\n")

    urls = []
    while True:
        line = input(f"URL {len(urls) + 1} (or Enter to finish): ").strip()
        if not line:
            break
        urls.append(line)

    if not urls:
        return urls, BACKFILL_BARCODES

    answer = input("\nFetch barcodes too? This adds one extra request per product "
                    "and can take much longer for large catalogs. (Y/n): ").strip().lower()
    backfill = answer not in ("n", "no")

    return urls, backfill


def print_summary_table(run_results: list):
    print("\n" + "=" * 78)
    print("RUN SUMMARY")
    print("=" * 78)
    header = f"{'Store':<25}{'Products':>10}{'CSV Rows':>10}{'Barcodes OK':>13}{'Total Time':>15}"
    print(header)
    print("-" * 78)
    for r in run_results:
        total_time = r["discovery_seconds"] + (r["backfill_seconds"] or 0)
        barcode_str = f"{r['barcode_success']}/{r['products_found']}" if r["barcode_success"] is not None else "n/a"
        print(
            f"{r['name']:<25}{r['products_found']:>10}{r['csv_rows']:>10}"
            f"{barcode_str:>13}{format_duration(total_time):>15}"
        )
    print("=" * 78)


def main():
    global BACKFILL_BARCODES

    urls, backfill = collect_urls_from_args_or_input()
    BACKFILL_BARCODES = backfill

    if not urls:
        print("No URLs provided. Exiting.")
        sys.exit(1)

    print(f"\nBarcode backfill: {'ON' if BACKFILL_BARCODES else 'OFF'}\n")

    run_started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    run_results = []

    for url in urls:
        if STOP_EVENT.is_set():
            print(f"\nSkipping remaining URLs (stopped by user).")
            break

        name = derive_store_name(url)
        store_start = time.time()
        try:
            products, stats = download_store_catalog(name, url)
            save_outputs(name, products)
            stats["name"] = name
            stats["url"] = url
            stats["csv_rows"] = len(flatten_products_to_rows(products))
            stats["status"] = "OK" if not STOP_EVENT.is_set() else "PARTIAL (stopped by user)"
        except Exception as e:
            print(f"[{name}] FAILED: {e}")
            stats = {
                "name": name, "url": url, "products_found": 0, "csv_rows": 0,
                "discovery_seconds": round(time.time() - store_start, 1),
                "barcode_success": None, "barcode_failed": None,
                "backfill_seconds": None, "status": f"FAILED: {e}",
            }
        run_results.append(stats)
        if not STOP_EVENT.is_set():
            time.sleep(REQUEST_DELAY)

    print_summary_table(run_results)

    # Save a machine-readable run log too, so past runs are reusable/reviewable
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_path = os.path.join(OUTPUT_DIR, "run_log.json")
    existing_log = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                existing_log = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing_log = []

    existing_log.append({"run_started_at": run_started_at, "stores": run_results})

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(existing_log, f, ensure_ascii=False, indent=2)

    print(f"\nRun log appended to: {log_path}")


if __name__ == "__main__":
    main()
