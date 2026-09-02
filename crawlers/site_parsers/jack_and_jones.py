"""
Parser for jackjones.in-style PDPs: a clean HTML table under the
"PRODUCT DESCRIPTION" accordion.

    <table class="product-specifications-table table--bordered">
      <tr>
        <td class="specification-label bold">Generic Name</td>
        <td class="specification-value">Jeans</td>
      </tr>
      ...
    </table>

Discovered via inspect_product_page.py against:
    https://www.jackjones.in/products/902636801-blue-dario-loose-fit-jeans

Note: this site's JSON-LD ProductGroup block has no "image" field, so
extract_images() in scrape_product_details.py automatically falls back
to og:image/twitter:image meta tags for this site - no extra work needed.
"""

from bs4 import BeautifulSoup


def parse(soup: BeautifulSoup) -> dict:
    attrs = {}

    table = soup.select_one("table.product-specifications-table")
    if table:
        for row in table.select("tr"):
            label_el = row.select_one(".specification-label")
            value_el = row.select_one(".specification-value")
            if label_el and value_el:
                label = label_el.get_text(strip=True)
                value = value_el.get_text(strip=True)
                if label and value:
                    attrs[label] = value

    return attrs
