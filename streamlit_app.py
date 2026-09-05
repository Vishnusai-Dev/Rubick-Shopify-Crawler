#!/usr/bin/env python3
"""
Catalog Crawler Control Panel
================================
Streamlit UI for running the crawl -> match -> detail-scrape pipeline
against any onboarded brand website. See README.md for the full manual.

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

for d in [CATALOG_DIR, SHOPPERSTOP_DIR, MATCHED_DIR, DETAILS_DIR]:
    os.makedirs(d, exist_ok=True)


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
# Subprocess runner with live log streaming
# =============================================================================

def run_command_streaming(cmd, cwd, log_placeholder):
    """Run a subprocess, streaming its output into a Streamlit placeholder
    line by line. Returns (returncode, full_output_text)."""
    process = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, universal_newlines=True,
    )

    output_lines = []
    for line in iter(process.stdout.readline, ""):
        output_lines.append(line)
        # Keep the log box readable - show the tail, not the whole growing history
        tail = "".join(output_lines[-300:])
        log_placeholder.code(tail, language=None)

    process.stdout.close()
    returncode = process.wait()
    return returncode, "".join(output_lines)


# =============================================================================
# UI
# =============================================================================

st.set_page_config(page_title="Catalog Crawler Control Panel", layout="wide")
st.title("Catalog Crawler Control Panel")

sites = load_sites()
site_names = [s["name"] for s in sites]

with st.sidebar:
    st.header("Website")
    mode = st.radio("Select a website or add a new one", ["Existing site", "Add new site"])

    if mode == "Existing site":
        if not site_names:
            st.warning("No sites registered yet. Switch to 'Add new site'.")
            st.stop()
        selected_name = st.selectbox("Site", site_names)
        site = next(s for s in sites if s["name"] == selected_name)
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
        new_platform = st.selectbox("Platform (best guess for now)",
                                     ["shopify_custom", "shopify_standard", "other"])
        if st.button("Register site"):
            if not new_name or not new_url:
                st.error("Name and URL are required.")
            elif new_name in site_names:
                st.error("A site with that name already exists.")
            else:
                domain = urlparse(new_url).netloc.replace("www.", "")
                sites.append({
                    "name": new_name, "domain": domain, "base_url": new_url.rstrip("/"),
                    "platform": new_platform, "detail_parser": "", "status": "new", "notes": "",
                })
                save_sites(sites)
                st.success(f"Registered '{new_name}'. Reload the page to select it.")
        st.stop()

st.markdown("---")

# --- Stage selection ---
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

# --- Stage 1 config ---
crawl_barcode = True
if do_crawl:
    st.subheader("2. Crawl settings")
    crawl_barcode = st.checkbox("Include barcode backfill during crawl", value=True,
                                 help="Needed for EAN matching in stage 2. Slower - skip only if you already have barcodes.")

# --- Stage 2 config ---
shopperstop_file_path = None
shopperstop_ean_col = "Active EAN"
crawled_csv_path = os.path.join(CATALOG_DIR, f"{derive_store_name(site['base_url'])}.csv") if mode == "Existing site" else None

if do_match:
    st.subheader("3. Shoppers Stop matching file")
    st.info(f"This file will be matched against the crawled catalog for "
            f"**{site['name']}** ({site['domain']}). Wrong brand? Change the "
            f"site in the sidebar first, then re-upload.")
    uploaded = st.file_uploader("Upload the Shoppers Stop SKU/EAN file for this brand (Excel or CSV)",
                                 type=["xlsx", "csv"])

    detected_ean_col = "Active EAN"
    if uploaded:
        shopperstop_file_path = os.path.join(SHOPPERSTOP_DIR, f"{site['name']}_master{os.path.splitext(uploaded.name)[1]}")
        with open(shopperstop_file_path, "wb") as f:
            f.write(uploaded.getbuffer())
        st.success(f"Saved: {shopperstop_file_path}")

        try:
            preview_df = pd.read_excel(shopperstop_file_path) if shopperstop_file_path.endswith(".xlsx") \
                else pd.read_csv(shopperstop_file_path)
            st.caption(f"**Columns found:** {list(preview_df.columns)}")
            ean_candidates = [c for c in preview_df.columns if "ean" in c.lower() or "barcode" in c.lower()]
            if len(ean_candidates) == 1:
                detected_ean_col = ean_candidates[0]
                st.caption(f"Auto-detected EAN column: **{detected_ean_col}**")
            elif len(ean_candidates) > 1:
                st.caption(f"Multiple possible EAN columns found: {ean_candidates} - confirm the right one below.")
        except Exception as e:
            st.warning(f"Could not preview file columns: {e}")

    shopperstop_ean_col = st.text_input("EAN column name in that file (edit if the auto-detected guess is wrong)",
                                         value=detected_ean_col)

    if not do_crawl:
        crawled_csv_path = st.text_input(
            "Path to already-crawled catalog CSV (since crawl stage is unchecked)",
            value=crawled_csv_path or "",
        )

# --- Stage 3 config (when running standalone, without stage 2 in this session) ---
matched_dir_for_site = os.path.join(MATCHED_DIR, site["name"]) if mode == "Existing site" else None
matched_xlsx_path = os.path.join(matched_dir_for_site, "matched.xlsx") if matched_dir_for_site else None

if do_details and not do_match:
    st.subheader("4. Matched file (Stage 2 is unchecked, so point Stage 3 at existing matched data)")

    default_path = matched_xlsx_path
    existing_found = default_path and os.path.exists(default_path)
    st.caption(
        f"Looking for: `{default_path}` - "
        + ("found on disk, will use it." if existing_found else "not found. Upload it below or fix the path.")
    )

    uploaded_matched = st.file_uploader(
        "Upload matched.xlsx (from a previous Stage 2 run / the download button)",
        type=["xlsx"],
    )
    matched_xlsx_path = st.text_input("Path to matched.xlsx", value=default_path or "")

    if uploaded_matched:
        os.makedirs(os.path.dirname(matched_xlsx_path) or matched_dir_for_site, exist_ok=True)
        with open(matched_xlsx_path, "wb") as f:
            f.write(uploaded_matched.getbuffer())
        st.success(f"Saved uploaded file to: {matched_xlsx_path}")

st.markdown("---")

# --- Run button ---
if st.button("Run selected stages", type="primary"):
    if do_match and not shopperstop_file_path:
        st.error("Please upload a Shoppers Stop file before running the match stage.")
        st.stop()
    if do_details and not do_match and not (matched_xlsx_path and os.path.exists(matched_xlsx_path)):
        st.error(f"No matched.xlsx found at: {matched_xlsx_path}. "
                 f"Upload it in section 4 above, or check 'Match against Shoppers Stop file' to generate it first.")
        st.stop()

    if matched_dir_for_site is None:
        matched_dir_for_site = os.path.join(MATCHED_DIR, site["name"])
    if matched_xlsx_path is None:
        matched_xlsx_path = os.path.join(matched_dir_for_site, "matched.xlsx")

    # --- Stage 1: Crawl ---
    if do_crawl:
        st.subheader("Stage 1: Crawling catalog")
        log_box = st.empty()
        cmd = [sys.executable, "shopify_catalog_downloader.py", site["base_url"]]
        cmd.append("--barcode" if crawl_barcode else "--no-barcode")
        returncode, _ = run_command_streaming(cmd, cwd=CRAWLERS_DIR, log_placeholder=log_box)
        if returncode != 0:
            st.error("Crawl stage failed - see log above.")
            st.stop()
        st.success("Crawl stage complete.")

        crawled_output_path = os.path.join(CATALOG_DIR, f"{derive_store_name(site['base_url'])}.csv")
        if os.path.exists(crawled_output_path):
            crawled_preview_df = pd.read_csv(crawled_output_path, dtype=str)
            st.write(f"**{len(crawled_preview_df)} rows crawled.** Preview:")
            st.dataframe(crawled_preview_df.head(20))
            with open(crawled_output_path, "rb") as f:
                st.download_button("Download crawled catalog CSV", f,
                                    file_name=f"{site['name']}_catalog.csv",
                                    key="download_crawled_csv")
        else:
            st.warning(f"Crawl reported success but no output file found at {crawled_output_path} - "
                       f"check the log above for '0 products found'.")

    # --- Stage 2: Match ---
    if do_match:
        st.subheader("Stage 2: Matching against Shoppers Stop")
        if not os.path.exists(crawled_csv_path):
            st.error(f"Crawled catalog CSV not found at: {crawled_csv_path}")
            st.stop()
        log_box = st.empty()
        cmd = [
            sys.executable, "match_catalog_by_ean.py",
            "--shopperstop-file", shopperstop_file_path,
            "--shopperstop-ean-col", shopperstop_ean_col,
            "--crawled-file", crawled_csv_path,
            "--crawled-barcode-col", "barcode",
            "--output-dir", matched_dir_for_site,
        ]
        returncode, _ = run_command_streaming(cmd, cwd=MATCHING_DIR, log_placeholder=log_box)
        if returncode != 0:
            st.error("Match stage failed - see log above.")
            st.stop()
        st.success("Match stage complete.")

        if os.path.exists(matched_xlsx_path):
            matched_df = pd.read_excel(matched_xlsx_path)
            st.write(f"**{len(matched_df)} matched rows.** Preview:")
            st.dataframe(matched_df.head(20))
            with open(matched_xlsx_path, "rb") as f:
                st.download_button("Download matched.xlsx", f, file_name=f"{site['name']}_matched.xlsx")

    # --- Stage 3: Detail scrape (matched only) ---
    if do_details:
        st.subheader("Stage 3: Scraping rich details (matched SKUs only)")
        if not os.path.exists(matched_xlsx_path):
            st.error(
                f"No matched.xlsx found at {matched_xlsx_path}. "
                f"Run the match stage first (or point stage 3 at an existing matched file - "
                f"not yet supported in this UI, run matching/match_catalog_by_ean.py manually)."
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
        log_box = st.empty()
        cmd = [sys.executable, "scrape_product_details.py", url_list_path,
               "--url-list", "--output", details_output]
        returncode, _ = run_command_streaming(cmd, cwd=CRAWLERS_DIR, log_placeholder=log_box)
        if returncode != 0:
            st.error("Detail scrape stage failed - see log above.")
            st.stop()
        st.success("Detail scrape stage complete.")

        if os.path.exists(details_output):
            details_df = pd.read_csv(details_output)
            st.write(f"**{len(details_df)} products with details scraped.** Preview:")
            st.dataframe(details_df.head(20))
            with open(details_output, "rb") as f:
                st.download_button("Download details CSV", f, file_name=f"{site['name']}_details.csv")

    st.balloons()
