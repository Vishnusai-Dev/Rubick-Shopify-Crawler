"""
Parser for thehouseofrare.com-style PDPs: tabbed panels, content is
<br/>-separated text inside a single <p>, not clean label/value elements.

    <div id="tab-description">...description + "- Bullet" features + SIZE note...</div>
    <div id="tab-page-1">...manufacturer name / Address - X / Country of Origin - X...</div>

Discovered via inspect_product_page.py against:
    https://thehouseofrare.com/products/kent-c-mens-shirt-purple
"""

import re
from bs4 import BeautifulSoup


def parse(soup: BeautifulSoup) -> dict:
    attrs = {}

    # --- "Description" tab: id="tab-description" ---
    desc_panel = soup.select_one("#tab-description .content-wrapper")
    if desc_panel:
        for br in desc_panel.find_all("br"):
            br.replace_with("\n")
        lines = [l.strip() for l in desc_panel.get_text("\n").split("\n") if l.strip()]

        description_lines = []
        features = []
        size_note = None
        in_size_section = False

        for line in lines:
            if line.upper() == "SIZE":
                in_size_section = True
                continue
            if in_size_section:
                size_note = line
                in_size_section = False
            elif line.startswith("-"):
                features.append(line.lstrip("- ").strip())
            else:
                description_lines.append(line)

        if description_lines:
            attrs["_description"] = " ".join(description_lines)
        if features:
            attrs["_features"] = "; ".join(features)
        if size_note:
            attrs["_size_note"] = size_note
            height_m = re.search(r"height\s+(\d+\s*cm)", size_note, re.IGNORECASE)
            chest_m = re.search(r"Chest-?\s*(\d+)", size_note, re.IGNORECASE)
            waist_m = re.search(r"Waist-?\s*(\d+)", size_note, re.IGNORECASE)
            hips_m = re.search(r"Hips-?\s*(\d+)", size_note, re.IGNORECASE)
            worn_size_m = re.search(r"wearing a size\s+(\w+)", size_note, re.IGNORECASE)
            if height_m:
                attrs["model_height"] = height_m.group(1)
            if chest_m:
                attrs["model_chest"] = chest_m.group(1)
            if waist_m:
                attrs["model_waist"] = waist_m.group(1)
            if hips_m:
                attrs["model_hips"] = hips_m.group(1)
            if worn_size_m:
                attrs["model_wearing_size"] = worn_size_m.group(1)

    # --- "Manufacturer Details" tab: id="tab-page-1" ---
    mfr_panel = soup.select_one("#tab-page-1 .content-wrapper") or soup.select_one("#tab-page-1")
    if mfr_panel:
        for br in mfr_panel.find_all("br"):
            br.replace_with("\n")
        lines = [l.strip() for l in mfr_panel.get_text("\n").split("\n") if l.strip()]

        for line in lines:
            if line.lower().startswith("address"):
                attrs["manufacturer_address"] = line.split("-", 1)[-1].strip()
            elif line.lower().startswith("country of origin"):
                attrs["country_of_origin"] = line.split("-", 1)[-1].strip()
            elif line.lower().startswith("mail id"):
                attrs["manufacturer_email"] = line.split(":", 1)[-1].strip()
            elif "manufacturer_name" not in attrs:
                attrs["manufacturer_name"] = line

    return attrs
