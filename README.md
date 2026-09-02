# Catalog Crawler System

A reusable system for crawling brand Shopify catalogs, matching them against
Shoppers Stop's SKU/EAN master lists, and scraping rich product attributes
(material, fit, dimensions, images, etc.) - only for products that actually
matched, to avoid wasting time scraping unmatched inventory.

Built for the Shoppers Stop workflow: ~200 brand websites, each needing the
same three-stage pipeline.

---

## Repository structure

```
├── config/
│   └── sites.yaml              <- registry of every onboarded brand website
├── crawlers/
│   ├── shopify_catalog_downloader.py   <- Stage 1: bulk catalog + barcode
│   ├── scrape_product_details.py       <- Stage 3: rich attributes (multi-site)
│   ├── inspect_product_page.py         <- onboarding diagnostic tool
│   └── site_parsers/                   <- one file per site's HTML structure
│       ├── celio.py
│       ├── house_of_rare.py
│       └── __init__.py                 <- registry, dispatches by domain
├── matching/
│   └── match_catalog_by_ean.py         <- Stage 2: EAN/barcode matching
├── data/                               <- NOT in git (see .gitignore)
│   ├── shopperstop_master/             <- uploaded Shoppers Stop files
│   ├── catalog_downloads/              <- raw crawled catalogs
│   ├── matched/                        <- matched/unmatched output per site
│   └── details/                        <- rich attribute output per site
├── streamlit_app.py                    <- the control panel UI
├── requirements.txt
└── README.md                           <- this file
```

**Why data/ isn't in git:** crawled catalogs can be large and are always
reproducible by re-running the crawler, and Shoppers Stop's files may be
confidential. Only code and config live in version control.

---

## Setup

```bash
git clone <your-repo-url>
cd <repo-name>
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running the control panel

```bash
streamlit run streamlit_app.py
```

This opens a browser tab where you can select a site, choose which stages
to run, upload the Shoppers Stop file, and download results - the sections
below explain what each stage does and how to use it both through the UI
and directly from the command line (useful for scripting/automation later).

---

## The three-stage pipeline

### Stage 1 - Crawl the complete website

Pulls every product from the brand's Shopify store via `/products.json`
(paginated), then backfills real `barcode` values per product (the bulk
listing endpoint often omits them - see `crawlers/shopify_catalog_downloader.py`
docstring for why).

**Via the UI:** check "Crawl complete catalog", select barcode on/off, hit Run.

**Via CLI:**
```bash
cd crawlers
python shopify_catalog_downloader.py "https://mybrand.com/" --barcode
```
Output: `data/catalog_downloads/<domain_slug>.csv` and `.json`.

This stage is the same for every brand - no per-site customization needed,
since it only uses Shopify's standard REST API.

### Stage 2 - Match against Shoppers Stop

Joins the crawled catalog to Shoppers Stop's SKU/EAN master file. The join
key is **EAN <-> barcode** - internal Material Numbers and the brand's own
Shopify SKUs use unrelated coding schemes and won't match directly.

**Via the UI:** check "Match against Shoppers Stop file", upload the file,
confirm the EAN column name, hit Run.

**Via CLI:**
```bash
cd matching
python match_catalog_by_ean.py \
    --shopperstop-file ../data/shopperstop_master/mybrand_master.xlsx \
    --shopperstop-ean-col "Active EAN" \
    --crawled-file ../data/catalog_downloads/mybrand_com.csv \
    --crawled-barcode-col barcode \
    --output-dir ../data/matched/mybrand
```
Output: `matched.xlsx`, `unmatched_shopperstop.xlsx`, `unmatched_crawled.xlsx`.

**If the Shoppers Stop file's column names vary by brand** (they might not
always be called "Active EAN"), just adjust `--shopperstop-ean-col` per run -
no code changes needed.

### Stage 3 - Scrape rich catalog details (matched SKUs only)

This is the optimization: instead of detail-scraping the entire catalog
(slow - one request per product), it only scrapes products that matched in
Stage 2. For a brand where only 30% of the catalog matches Shoppers Stop's
list, this cuts detail-scraping time by ~70%.

**Via the UI:** check "Scrape rich details (matched SKUs only)" - it
automatically reads `matched.xlsx` from Stage 2 and builds the URL list
from matched handles. Requires Stage 2 to have run first (same session or
a previous one, as long as `matched.xlsx` exists).

**Via CLI:**
```bash
cd crawlers
# Build a URL list from matched handles first (the UI does this for you)
python scrape_product_details.py ../data/matched/mybrand/matched_product_urls.txt \
    --url-list --output ../data/details/mybrand_details.csv
```

**This is the stage that needs a site-specific parser** - see below.

---

## Onboarding a New Website

Follow this every time a new brand comes in. Steps 1-2 always apply; step 3
only if you want rich attributes (Stage 3), not just the bulk catalog.

### 1. Register the site

Either use the Streamlit sidebar ("Add new site" - name, URL, best-guess
platform), or add an entry directly to `config/sites.yaml`:

```yaml
  - name: new_brand
    domain: newbrand.com
    base_url: https://newbrand.com
    platform: shopify_custom   # see platform types below
    detail_parser: ""          # filled in during step 3
    status: new
    notes: ""
```

### 2. Confirm it's Shopify and run Stage 1

Quick check: open `https://newbrand.com/products/<any-handle>.json` in a
browser. Raw JSON = Shopify, standard flow applies. HTML/404 = not Shopify,
this system's Stage 1/2 won't work as-is (flag `platform: other` and treat
as a bespoke job).

If Shopify, just run Stage 1 - no customization needed regardless of theme,
since `/products.json` is a standard Shopify endpoint.

### 3. (Optional) Build a rich-attribute parser for Stage 3

Only needed if you want material/fit/dimensions/etc, not just core catalog
fields. Skip this and leave `detail_parser: ""` if the brand's Stage 1 data
(title, price, SKU, barcode, images) is enough for now - `status: crawl_only`.

**a. Run the diagnostic:**
```bash
cd crawlers
python inspect_product_page.py "https://newbrand.com/products/<some-handle>"
```
This checks three things and tells you which applies:
- Framework-embedded JSON (Next.js `__NEXT_DATA__`, etc.) - rare but ideal if present
- JSON-LD structured data (`<script type="application/ld+json">`) - almost always present, good for images/description
- Visible HTML sections (pass search terms as a second arg if the default
  headings like "Product Details" don't match this site's tab names, e.g.
  `python inspect_product_page.py <url> "Specifications,Manufacturer Info"`)

**b. Write a parser module** in `crawlers/site_parsers/new_brand.py`:
```python
from bs4 import BeautifulSoup

def parse(soup: BeautifulSoup) -> dict:
    attrs = {}
    # ... extract label/value pairs based on what step (a) showed you ...
    return attrs
```
Use `crawlers/site_parsers/celio.py` (label/value block pattern) or
`house_of_rare.py` (tabbed panel pattern) as a starting template - most
sites resemble one of these two shapes.

**c. Register it** in `config/sites.yaml`:
```yaml
    detail_parser: new_brand
    status: active
```

That's it - `scrape_product_details.py` picks it up automatically via the
`site_parsers` registry, no other code changes needed.

### Platform types reference

| platform | meaning |
|---|---|
| `shopify_standard` | `/products.json` works, no custom parser needed (rare - most themes hide metafields from the API) |
| `shopify_custom` | Shopify, but rich attributes need a dedicated parser (the common case) |
| `other` | Not Shopify - Stage 1/2 need a different approach entirely, treat as a one-off |

---

## Politeness / anti-blocking behavior (already built in)

Both `shopify_catalog_downloader.py` and `scrape_product_details.py` share
the same safety features, tuned to avoid getting an IP blocked across ~200
sites of varying strictness:

- Randomized jitter between requests (not robotic, fixed timing)
- A circuit breaker: repeated 403/429 responses trigger an automatic pause
- Multi-pass retries: failed/rate-limited items get retried in later passes
  instead of being permanently dropped
- Graceful Ctrl+C: stops cleanly and saves whatever was already fetched,
  instead of hanging or losing all progress
- Unresolved items are always saved to a CSV, never silently lost

If a specific site is stricter than others (frequent circuit-breaker trips),
lower `WORKERS` and raise `COOLDOWN_SECONDS` at the top of the relevant
script for that run.

---

## Pushing this repo to GitHub

```bash
cd <repo-folder>
git init
git add .
git commit -m "Initial catalog crawler system"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

For future changes (new site parsers, tuning), just commit and push as usual
- `data/` stays untracked so runs don't bloat the repo.
