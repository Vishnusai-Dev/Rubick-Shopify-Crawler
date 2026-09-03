"""
NNNow Product Crawler — Streamlit app.

Paste one or more NNNow product URLs, crawl them, and browse/download the
results. Reuses the extraction logic from nnnow_crawler.py (same folder) —
no logic is duplicated here, this file is purely the UI layer.

Run:
    streamlit run streamlit_app.py

Requires nnnow_crawler.py to sit in the same directory.
"""

import json
import time

import pandas as pd
import streamlit as st

from nnnow_crawler import fetch, parse_product_page, HEADERS
import requests

st.set_page_config(page_title="NNNow Product Crawler", layout="wide")

st.title("NNNow Product Crawler")
st.caption(
    "Pulls product data straight from NNNow's own embedded `window.DATA` JSON — "
    "sizes, stock, pricing, images, composition, and size chart — no DOM guesswork."
)

# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    delay = st.slider(
        "Delay between requests (seconds)", 0.5, 5.0, 1.5, 0.5,
        help="Polite delay to avoid rate-limiting / IP blocks on larger runs.",
    )
    uploaded_file = st.file_uploader("...or upload a .txt file of URLs (one per line)", type=["txt"])

url_text = st.text_area(
    "Product URLs (one per line)",
    height=150,
    placeholder="https://tommyhilfiger.nnnow.com/tommy-hilfiger-solid-slim-fit-cotton-t-shirt-95K1AKO6CF8",
)

urls = [u.strip() for u in url_text.splitlines() if u.strip()]
if uploaded_file is not None:
    file_urls = [u.strip() for u in uploaded_file.read().decode("utf-8").splitlines() if u.strip()]
    urls = list(dict.fromkeys(urls + file_urls))  # merge, de-dupe, keep order

run = st.button("Crawl", type="primary", disabled=not urls)

# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------
if run:
    session = requests.Session()
    results = []
    progress = st.progress(0.0, text="Starting...")

    for i, url in enumerate(urls):
        progress.progress(i / len(urls), text=f"Fetching {i + 1}/{len(urls)}: {url}")
        try:
            html = fetch(url, session)
            data = parse_product_page(html, url)
        except requests.RequestException as e:
            data = {"source_url": url, "error": str(e)}
        results.append(data)
        if i < len(urls) - 1:
            time.sleep(delay)

    progress.progress(1.0, text="Done")
    st.session_state["results"] = results

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
results = st.session_state.get("results", [])

if results:
    ok = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]

    st.subheader(f"Results — {len(ok)} succeeded, {len(failed)} failed")

    if ok:
        # Flat summary table: one row per product, sizes/images collapsed to counts
        summary_rows = []
        for r in ok:
            summary_rows.append({
                "Title": r.get("title"),
                "Brand": r.get("brand"),
                "Style ID": r.get("style_id"),
                "MRP": r.get("mrp"),
                "Selling Price": r.get("selling_price"),
                "Discount %": r.get("discount_pct"),
                "In Stock": r.get("in_stock"),
                "# Sizes": len(r.get("sizes", [])) if isinstance(r.get("sizes"), list) else None,
                "# Images": len(r.get("images", [])) if isinstance(r.get("images"), list) else None,
                "Has Size Chart": "size_chart" in r,
                "URL": r.get("source_url"),
            })
        df = pd.DataFrame(summary_rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Per-product drilldown
        st.subheader("Product details")
        for r in ok:
            with st.expander(f"{r.get('brand', '')} — {r.get('title', r.get('source_url'))}"):
                col1, col2 = st.columns([1, 2])

                with col1:
                    images = r.get("images") or []
                    if images and images[0].get("medium"):
                        st.image(images[0]["medium"], width=250)

                with col2:
                    st.markdown(f"**MRP:** ₹{r.get('mrp')}  |  **Selling Price:** ₹{r.get('selling_price')}  "
                                f"|  **Discount:** {r.get('discount_pct')}%")
                    st.markdown(f"**Description:** {r.get('description', '')}")

                    sizes = r.get("sizes")
                    if sizes:
                        st.markdown("**Sizes & stock**")
                        st.dataframe(pd.DataFrame(sizes), use_container_width=True, hide_index=True)

                    size_chart = r.get("size_chart")
                    if size_chart:
                        st.markdown("**Size chart (inch)**")
                        if size_chart.get("measurements_inch"):
                            st.dataframe(pd.DataFrame(size_chart["measurements_inch"]),
                                         use_container_width=True, hide_index=True)
                        if size_chart.get("chart_image"):
                            st.image(size_chart["chart_image"], width=300)

                st.json(r, expanded=False)

        # Downloads
        st.subheader("Download")
        col_a, col_b = st.columns(2)
        with col_a:
            st.download_button(
                "Download JSON (all fields, per product)",
                data="\n".join(json.dumps(r, ensure_ascii=False) for r in results),
                file_name="nnnow_products.jsonl",
                mime="application/jsonl",
            )
        with col_b:
            st.download_button(
                "Download summary CSV",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name="nnnow_products_summary.csv",
                mime="text/csv",
            )

    if failed:
        st.subheader("Failed")
        st.dataframe(pd.DataFrame(failed), use_container_width=True, hide_index=True)
else:
    st.info("Paste URLs above and click Crawl to get started.")
