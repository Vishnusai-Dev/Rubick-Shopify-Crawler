"""
Site parser registry.

Loads the site list from config/sites.yaml and dynamically imports the
matching parser module (by the `detail_parser` field) for a given URL's
domain. This is the single place that connects "a URL" to "the function
that knows how to read its rich attribute HTML".

Usage:
    from site_parsers import get_parser_for_url
    parser_fn = get_parser_for_url("https://celio.in/products/...")
    attrs = parser_fn(soup)
"""

import importlib
import os
from urllib.parse import urlparse

import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "sites.yaml")
_warned_domains = set()


def _load_sites():
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sites", [])


def get_site_config(url: str) -> dict:
    """Return the sites.yaml entry matching this URL's domain, or None."""
    domain = urlparse(url).netloc.lower().replace("www.", "")
    for site in _load_sites():
        if site.get("domain", "").lower() in domain:
            return site
    return None


def get_parser_for_url(url: str):
    """Return a callable parse(soup) -> dict for this URL's site, or a
    generic fallback (tries every known parser) if none is registered."""
    site = get_site_config(url)

    if site and site.get("detail_parser"):
        module_name = site["detail_parser"]
        try:
            module = importlib.import_module(f"site_parsers.{module_name}")
            return module.parse
        except ImportError as e:
            print(f"  !! Failed to load parser '{module_name}' for {site['domain']}: {e}")

    domain = urlparse(url).netloc.lower().replace("www.", "")
    if domain not in _warned_domains:
        _warned_domains.add(domain)
        if site:
            print(f"  !! Site '{domain}' is registered but has no detail_parser set "
                  f"(status: {site.get('status', 'unknown')}). "
                  f"See README.md -> 'Onboarding a New Website'.")
        else:
            print(f"  !! Domain '{domain}' is not in config/sites.yaml at all. "
                  f"Add it there first, then run inspect_product_page.py against it. "
                  f"See README.md -> 'Onboarding a New Website'.")

    def generic_fallback(soup):
        for site_entry in _load_sites():
            parser_name = site_entry.get("detail_parser")
            if not parser_name:
                continue
            try:
                module = importlib.import_module(f"site_parsers.{parser_name}")
                result = module.parse(soup)
                if result:
                    return result
            except Exception:
                continue
        return {}

    return generic_fallback
