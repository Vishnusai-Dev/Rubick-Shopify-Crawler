"""
Parser for celio.in-style PDPs: structured <div class="block"> pairs.

    <div class="block">
      <div class="pd_label">Primary Material</div>
      <div class="pd_text">Poly-Cotton</div>
    </div>

Discovered via inspect_product_page.py against:
    https://celio.in/products/men-green-poly-cotton-solid-t-shirts-megaufre-green-mid
"""

import re
from bs4 import BeautifulSoup


def parse(soup: BeautifulSoup) -> dict:
    attrs = {}

    # 1. Structured "Product Details" / "Manufacture Details" style blocks
    for block in soup.select("div.block"):
        label_el = block.select_one(".pd_label")
        value_el = block.select_one(".pd_text")
        if label_el and value_el:
            label = label_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            if label and value:
                attrs[label] = value

    # 2. "Product Overview" bullet list: <li>Label: Value</li>
    #    Sometimes has extra attributes (e.g. Occasion, Care) not in the table above.
    overview = soup.select_one("div.c_product_descp") or soup.select_one(".c_product_descp")
    if overview:
        for li in overview.select("li"):
            text = li.get_text(strip=True)
            if ":" in text:
                label, _, value = text.partition(":")
                label, value = label.strip(), value.strip()
                if label and value and label not in attrs:
                    attrs[label] = value

        paragraphs = [p.get_text(strip=True) for p in overview.select("p")]
        if paragraphs:
            attrs["_description"] = " ".join(paragraphs)

    # 3. "Fabric & Care" instructions - usually a plain list, not label:value pairs
    for heading in soup.find_all(string=re.compile(r"Fabric\s*&\s*Care", re.IGNORECASE)):
        heading_el = heading.find_parent()
        if not heading_el:
            continue
        item = heading_el.find_parent(class_=re.compile("accordion-single-item"))
        if item:
            content = item.select_one(".accordion-single-content")
            if content:
                lines = [t for t in content.stripped_strings if t]
                if lines:
                    attrs["_fabric_care_instructions"] = "; ".join(lines)
        break

    return attrs
