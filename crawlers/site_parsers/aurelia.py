"""
Parser for shopforaurelia.com-style PDPs: a clean <ul> list under the
"Product Details" accordion, each item a label/value div pair.

    <ul class="additional_content">
      <li>
        <div class="additional_title">Fabric Detail</div>
        <div class="additional_content">Rayon</div>
      </li>
      ...
    </ul>

Discovered via inspect_product_page.py against:
    https://shopforaurelia.com/products/black-floral-printed-rayon-straight-kurta-with-palazzo-and-stole-set-as16790-511047

Note: confirmed via direct browser test that this site loads fine from a
residential IP - failures from a datacenter/cloud IP (e.g. Streamlit Cloud)
are very likely bot-protection blocking that IP range specifically, not a
real 404 or a parser problem. See README.md -> "A note on cloud hosting
and IP blocking" if this parser still returns nothing when run from a
cloud-hosted deployment.
"""

from bs4 import BeautifulSoup


def parse(soup: BeautifulSoup) -> dict:
    attrs = {}

    for li in soup.select("ul.additional_content li"):
        label_el = li.select_one(".additional_title")
        value_el = li.select_one(".additional_content")
        if label_el and value_el:
            label = label_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            if label and value:
                attrs[label] = value

    return attrs
