#!/usr/bin/env python3
"""
Catalog Crawler Control Panel
================================
Streamlit UI for running crawl pipelines against onboarded brand websites.

Two distinct workflows, split by tab:
  - Shopify Sites: crawl -> match -> detail-scrape pipeline (bulk
    /products.json crawl, EAN match against Shoppers Stop, then rich detail
    scrape on matched SKUs only).
  - Non-Shopify Sites: custom storefronts (e.g. NNNow) with no bulk catalog
    endpoint. You give it product page URLs directly; it pulls each page's
    own embedded product JSON (sizes, stock, pricing, images, size chart).

See README.md for the full manual.

Run with:
    streamlit run streamlit_app.py
"""

import os
import re
import subprocess
import sys
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
import yaml

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(REPO_ROOT, "config", "sites.yaml")
CRAWLERS_DIR = os.path.join(REPO_ROOT, "crawlers")
MATCHING_DIR = os.path.join(REPO_ROOT, "matching")
DATA_DIR = os.path.join(REPO_ROOT, "data")
CATALOG_DIR = os.path.join(DATA_DIR, "catalog_downloads")
SHOPPERSTOP_DIR = os.path.join(DATA_DIR, "shopperstop_master")
MATCHED_DIR = os.path.join(DATA_DIR, "matched")
DETAILS_DIR = os.path.join(DATA_DIR, "details")
NONSHOPIFY_DIR = os.path.join(DATA_DIR, "non_shopify_details")

for d in [CATALOG_DIR, SHOPPERSTOP_DIR, MATCHED_DIR, DETAILS_DIR, NONSHOPIFY_DIR]:
    os.makedirs(d, exist_ok=True)

# Platform values that use the Shopify 3-stage pipeline. Anything else
# (e.g. "custom_react") is treated as Non-Shopify and uses the direct
# URL-crawl workflow instead.
SHOPIFY_PLATFORMS = {"shopify_custom", "shopify_standard"}

PROGRESS_RE = re.compile(r"PROGRESS\s+(\d+)\s*/\s*(\d+)")


# =============================================================================
# Site registry helpers
# =============================================================================

def load_sites():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {"sites": []}
    return data.get("sites", [])


def save_sites(sites):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({"sites": sites}, f, sort_keys=False, default_flow_style=False)


def derive_store_name(url: str) -> str:
    """Same slug logic as shopify_catalog_downloader.py, so we know where
    it will save its output CSV."""
    netloc = urlparse(url).netloc or url
    netloc = netloc.replace("www.", "")
    name = re.sub(r"[^a-zA-Z0-9]+", "_", netloc).strip("_").lower()
    return name or "store"


# =============================================================================
# Subprocess runner with live progress + collapsible logs
# =============================================================================

def run_command_streaming(cmd, cwd, progress_bar=None, status_text=None, log_container=None):
    """Run a subprocess, streaming its output.

    If the process prints lines matching "PROGRESS i/n", progress_bar and
    status_text are updated with real, exact progress. If it never prints
    that marker, progress_bar is left in an indeterminate "running" state
    instead of sitting frozen at 0% for the whole run.

    Full output still streams into log_container (an st.empty placeholder),
    kept in a collapsed expander by the caller so the UI isn't dominated by
    a scrolling wall of text.

    Returns (returncode, full_output_text).
    """
    process = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, universal_newlines=True,
    )

    output_lines = []
    saw_progress = False

    for line in iter(process.stdout.readline, ""):
        output_lines.append(line)

        m = PROGRESS_RE.search(line)
        if m and progress_bar is not None:
            saw_progress = True
            i, n = int(m.group(1)), max(int(m.group(2)), 1)
            progress_bar.progress(min(i / n, 1.0))
            if status_text is not None:
                status_text.caption(line.strip())
        elif not saw_progress and status_text is not None and line.strip():
            # No progress marker seen yet - at least show the latest line so
            # the UI doesn't look frozen while work is happening.
            status_text.caption(line.strip())

        if log_container is not None:
            log_container.code("".join(output_lines[-300:]), language=None)

    process.stdout.close()
    returncode = process.wait()

    if progress_bar is not None:
        progress_bar.progress(1.0)

    return returncode, "".join(output_lines)


def run_stage(label, cmd, cwd):
    """Wrap a pipeline stage in a status block with a real progress bar and
    a collapsed log expander, instead of an always-visible scrolling box."""
    st.markdown(f"**{label}**")
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    log_expander = st.expander("Show logs", expanded=False)
    with log_expander:
        log_placeholder = st.empty()

    returncode, output = run_command_streaming(
        cmd, cwd=cwd, progress_bar=progress_bar, status_text=status_text,
        log_container=log_placeholder,
    )

    if returncode != 0:
        status_text.empty()
        st.error(f"{label} failed - see logs above.")
    else:
        status_text.empty()
        st.success(f"{label} complete.")

    return returncode, output


# =============================================================================
# UI
# =============================================================================

st.set_page_config(page_title="Catalog Crawler Control Panel", layout="wide")
st.title("Catalog Crawler Control Panel")

tab_shopify, tab_non_shopify = st.tabs(["Shopify Sites", "Non-Shopify Sites"])

# =============================================================================
# TAB 1: Shopify pipeline (crawl -> match -> detail-scrape)
# =============================================================================
with tab_shopify:
    sites = load_sites()
    shopify_sites = [s for s in sites if s.get("platform") in SHOPIFY_PLATFORMS]
    shopify_site_names = [s["name"] for s in shopify_sites]

    with st.sidebar:
        st.header("Shopify Website")
        mode = st.radio("Select a website or add a new one", ["Existing site", "Add new site"], key="shopify_mode")

        site = None
        add_new_stopped = False

        if mode == "Existing site":
            if not shopify_site_names:
                st.warning("No Shopify sites registered yet. Switch to 'Add new site'.")
            else:
                selected_name = st.selectbox("Site", shopify_site_names)
                site = next(s for s in shopify_sites if s["name"] == selected_name)
                st.caption(f"**Domain:** {site['domain']}")
                st.caption(f"**Platform:** {site['platform']}")
                st.caption(f"**Status:** {site['status']}")
                if site.get("notes"):
                    st.caption(f"**Notes:** {site['notes']}")
                if not site.get("detail_parser") and site["platform"] == "shopify_custom":
                    st.warning(
                        "This site has no detail parser registered yet - Stage 3 "
                        "(rich attribute scraping) will find little or nothing. "
                        "See README.md -> 'Onboarding a New Website'."
                    )
        else:
            st.info("This registers the site so the pipeline knows about it. "
                    "You'll still need to run inspect_product_page.py yourself "
                    "and (if needed) write a parser - see README.md.")
            new_name = st.text_input("Internal name (lowercase, underscores)", placeholder="e.g. new_brand")
            new_url = st.text_input("Base URL", placeholder="https://newbrand.com/")
            new_platform = st.selectbox("Platform", ["shopify_custom", "shopify_standard"])
            if st.button("Register site"):
                if not new_name or not new_url:
                    st.error("Name and URL are required.")
                elif new_name in [s["name"] for s in sites]:
                    st.error("A site with that name already exists.")
                else:
                    domain = urlparse(new_url).netloc.replace("www.", "")
                    sites.append({
                        "name": new_name, "domain": domain, "base_url": new_url.rstrip("/"),
                        "platform": new_platform, "detail_parser": "", "status": "new", "notes": "",
                    })
                    save_sites(sites)
                    st.success(f"Registered '{new_name}'. Reload the page to select it.")
            add_new_stopped = True

    if add_new_stopped:
        st.info("Registering a new site - switch back to 'Existing site' in the sidebar once done.")
    elif site is None:
        st.info("Register a Shopify site in the sidebar to get started.")
    else:
        st.markdown("---")

        st.subheader("1. Choose which stages to run")
        col1, col2, col3 = st.columns(3)
        with col1:
            do_crawl = st.checkbox("Crawl complete catalog", value=True,
                                    help="Bulk /products.json crawl + barcode backfill for this site")
        with col2:
            do_match = st.checkbox("Match against Shoppers Stop file", value=True,
                                    help="Join the crawled catalog to a Shoppers Stop SKU/EAN file on barcode")
        with col3:
            do_details = st.checkbox("Scrape rich details (matched SKUs only)", value=True,
                                      help="Only scrapes products that matched in stage 2 - this is the optimization")

        st.markdown("---")

        crawl_barcode = True
        if do_crawl:
            st.subheader("2. Crawl settings")
            crawl_barcode = st.checkbox("Include barcode backfill during crawl", value=True,
                                         help="Needed for EAN matching in stage 2. Slower - skip only if you already have barcodes.")

        shopperstop_file_path = None
        shopperstop_ean_col = "Active EAN"
        crawled_csv_path = os.path.join(CATALOG_DIR, f"{derive_store_name(site['base_url'])}.csv")

        if do_match:
            st.subheader("3. Shoppers Stop matching file")
            uploaded = st.file_uploader("Upload the Shoppers Stop SKU/EAN file for this brand (Excel or CSV)",
                                         type=["xlsx", "csv"])
            shopperstop_ean_col = st.text_input("EAN column name in that file", value="Active EAN")
            if uploaded:
                shopperstop_file_path = os.path.join(
                    SHOPPERSTOP_DIR, f"{site['name']}_master{os.path.splitext(uploaded.name)[1]}")
                with open(shopperstop_file_path, "wb") as f:
                    f.write(uploaded.getbuffer())
                st.success(f"Saved: {shopperstop_file_path}")

            if not do_crawl:
                crawled_csv_path = st.text_input(
                    "Path to already-crawled catalog CSV (since crawl stage is unchecked)",
                    value=crawled_csv_path or "",
                )

        matched_dir_for_site = os.path.join(MATCHED_DIR, site["name"])
        matched_xlsx_path = os.path.join(matched_dir_for_site, "matched.xlsx")

        if do_details and not do_match:
            st.subheader("4. Matched file (Stage 2 is unchecked, so point Stage 3 at existing matched data)")
            existing_found = os.path.exists(matched_xlsx_path)
            st.caption(
                f"Looking for: `{matched_xlsx_path}` - "
                + ("found on disk, will use it." if existing_found else "not found. Upload it below or fix the path.")
            )
            uploaded_matched = st.file_uploader(
                "Upload matched.xlsx (from a previous Stage 2 run / the download button)", type=["xlsx"])
            matched_xlsx_path = st.text_input("Path to matched.xlsx", value=matched_xlsx_path)
            if uploaded_matched:
                os.makedirs(os.path.dirname(matched_xlsx_path) or matched_dir_for_site, exist_ok=True)
                with open(matched_xlsx_path, "wb") as f:
                    f.write(uploaded_matched.getbuffer())
                st.success(f"Saved uploaded file to: {matched_xlsx_path}")

        st.markdown("---")

        if st.button("Run selected stages", type="primary"):
            if do_match and not shopperstop_file_path:
                st.error("Please upload a Shoppers Stop file before running the match stage.")
                st.stop()
            if do_details and not do_match and not (matched_xlsx_path and os.path.exists(matched_xlsx_path)):
                st.error(f"No matched.xlsx found at: {matched_xlsx_path}. "
                         f"Upload it in section 4 above, or check 'Match against Shoppers Stop file' to generate it first.")
                st.stop()

            # --- Stage 1: Crawl ---
            if do_crawl:
                cmd = [sys.executable, "shopify_catalog_downloader.py", site["base_url"]]
                cmd.append("--barcode" if crawl_barcode else "--no-barcode")
                returncode, _ = run_stage("Stage 1: Crawling catalog", cmd, cwd=CRAWLERS_DIR)
                if returncode != 0:
                    st.stop()

            # --- Stage 2: Match ---
            if do_match:
                if not os.path.exists(crawled_csv_path):
                    st.error(f"Crawled catalog CSV not found at: {crawled_csv_path}")
                    st.stop()
                cmd = [
                    sys.executable, "match_catalog_by_ean.py",
                    "--shopperstop-file", shopperstop_file_path,
                    "--shopperstop-ean-col", shopperstop_ean_col,
                    "--crawled-file", crawled_csv_path,
                    "--crawled-barcode-col", "barcode",
                    "--output-dir", matched_dir_for_site,
                ]
                returncode, _ = run_stage("Stage 2: Matching against Shoppers Stop", cmd, cwd=MATCHING_DIR)
                if returncode != 0:
                    st.stop()

                if os.path.exists(matched_xlsx_path):
                    matched_df = pd.read_excel(matched_xlsx_path)
                    st.write(f"**{len(matched_df)} matched rows.** Preview:")
                    st.dataframe(matched_df.head(20), use_container_width=True)
                    with open(matched_xlsx_path, "rb") as f:
                        st.download_button("Download matched.xlsx", f, file_name=f"{site['name']}_matched.xlsx")

            # --- Stage 3: Detail scrape (matched only) ---
            if do_details:
                if not os.path.exists(matched_xlsx_path):
                    st.error(
                        f"No matched.xlsx found at {matched_xlsx_path}. "
                        f"Run the match stage first (or point stage 3 at an existing matched file above)."
                    )
                    st.stop()

                matched_df = pd.read_excel(matched_xlsx_path)
                handle_col = "handle" if "handle" in matched_df.columns else "handle_crawled"
                if handle_col not in matched_df.columns:
                    st.error(f"Could not find a 'handle' column in matched.xlsx. "
                             f"Columns present: {list(matched_df.columns)}")
                    st.stop()

                handles = matched_df[handle_col].dropna().unique().tolist()
                url_list_path = os.path.join(matched_dir_for_site, "matched_product_urls.txt")
                with open(url_list_path, "w", encoding="utf-8") as f:
                    for h in handles:
                        f.write(f"{site['base_url'].rstrip('/')}/products/{h}\n")

                st.write(f"Scraping details for {len(handles)} matched products only "
                         f"(instead of the full catalog - this is the optimization).")

                details_output = os.path.join(DETAILS_DIR, f"{site['name']}_details.csv")
                cmd = [sys.executable, "scrape_product_details.py", url_list_path,
                       "--url-list", "--output", details_output]
                returncode, _ = run_stage("Stage 3: Scraping rich details", cmd, cwd=CRAWLERS_DIR)
                if returncode != 0:
                    st.stop()

                if os.path.exists(details_output):
                    details_df = pd.read_csv(details_output)
                    st.write(f"**{len(details_df)} products with details scraped.** Preview:")
                    st.dataframe(details_df.head(20), use_container_width=True)
                    with open(details_output, "rb") as f:
                        st.download_button("Download details CSV", f, file_name=f"{site['name']}_details.csv")

            st.balloons()


# =============================================================================
# TAB 2: Non-Shopify pipeline (direct product URL crawl)
# =============================================================================
with tab_non_shopify:
    st.caption(
        "For custom storefronts with no bulk catalog endpoint (e.g. NNNow's React app). "
        "Paste product page URLs directly - each page's own embedded product data "
        "(sizes, stock, pricing, images, composition, size chart) is pulled out. "
        "No Shopify-style crawl/match staging needed here."
    )

    col_left, col_right = st.columns([2, 1])
    with col_left:
        url_text = st.text_area(
            "Product URLs (one per line)", height=180,
            placeholder="https://tommyhilfiger.nnnow.com/tommy-hilfiger-solid-slim-fit-cotton-t-shirt-95K1AKO6CF8",
        )
    with col_right:
        uploaded_url_file = st.file_uploader("...or upload a .txt file of URLs", type=["txt"], key="ns_upload")
        delay = st.slider("Delay between requests (sec)", 0.5, 5.0, 1.5, 0.5, key="ns_delay")
        run_name = st.text_input("Save under name", value="nnnow_run",
                                  help="Used to name the output files, e.g. nnnow_run.csv / .jsonl")

    urls = [u.strip() for u in url_text.splitlines() if u.strip()]
    if uploaded_url_file is not None:
        file_urls = [u.strip() for u in uploaded_url_file.read().decode("utf-8").splitlines() if u.strip()]
        urls = list(dict.fromkeys(urls + file_urls))  # merge, de-dupe, keep order

    st.caption(f"{len(urls)} URL(s) ready" if urls else "Paste or upload URLs to enable the crawl button")

    if st.button("Crawl", type="primary", disabled=not urls, key="ns_crawl_button"):
        url_list_path = os.path.join(NONSHOPIFY_DIR, f"{run_name}_urls.txt")
        with open(url_list_path, "w", encoding="utf-8") as f:
            f.write("\n".join(urls))

        output_csv = os.path.join(NONSHOPIFY_DIR, f"{run_name}.csv")
        output_jsonl = os.path.join(NONSHOPIFY_DIR, f"{run_name}.jsonl")

        cmd = [
            sys.executable, "nnnow_crawler.py", url_list_path, "--url-list",
            "--output", output_csv, "--delay", str(delay),
        ]
        returncode, _ = run_stage("Crawling product pages", cmd, cwd=CRAWLERS_DIR)

        if returncode == 0 and os.path.exists(output_csv):
            df = pd.read_csv(output_csv)
            st.session_state["ns_results_df"] = df
            st.session_state["ns_output_csv"] = output_csv
            st.session_state["ns_output_jsonl"] = output_jsonl

    df = st.session_state.get("ns_results_df")
    if df is not None:
        succeeded = df[df["error"].fillna("") == ""]
        failed = df[df["error"].fillna("") != ""]

        st.subheader(f"Results - {len(succeeded)} succeeded, {len(failed)} failed")
        st.dataframe(succeeded, use_container_width=True, hide_index=True)

        col_a, col_b = st.columns(2)
        with col_a:
            with open(st.session_state["ns_output_csv"], "rb") as f:
                st.download_button("Download summary CSV", f,
                                    file_name=os.path.basename(st.session_state["ns_output_csv"]))
        with col_b:
            if os.path.exists(st.session_state["ns_output_jsonl"]):
                with open(st.session_state["ns_output_jsonl"], "rb") as f:
                    st.download_button("Download full detail JSONL (sizes, images, size chart, etc.)", f,
                                        file_name=os.path.basename(st.session_state["ns_output_jsonl"]))

        if len(failed):
            st.subheader("Failed")
            st.dataframe(failed[["source_url", "error"]], use_container_width=True, hide_index=True)
