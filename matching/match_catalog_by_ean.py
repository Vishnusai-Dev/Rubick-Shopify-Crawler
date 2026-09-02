#!/usr/bin/env python3
"""
Match a crawled catalog against the Shoppers Stop SKU/EAN master list.

Joins on EAN (Shoppers Stop's "Active EAN" column) <-> barcode (our
crawler's "barcode" column) - these are the only reliable common key,
since Shoppers Stop's internal Material Numbers and the brand's own
Shopify SKUs use unrelated coding schemes.

Usage:
    pip install pandas openpyxl
    python match_catalog_by_ean.py \\
        --shopperstop-file shopperstop_master/raymond_materials.xlsx \\
        --shopperstop-ean-col "Active EAN" \\
        --crawled-file ../data/catalog_downloads/myraymond_com.csv \\
        --crawled-barcode-col barcode \\
        --output-dir ../data/matched/raymond
"""

import argparse
import os

import pandas as pd


def load_any(path: str) -> pd.DataFrame:
    if path.lower().endswith(".csv"):
        return pd.read_csv(path, dtype=str)
    return pd.read_excel(path, dtype=str)


def normalize_ean(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.strip()
    s = s.str.replace(r"\.0$", "", regex=True)
    s = s.replace({"nan": None, "None": None, "": None})
    return s


def match(shopperstop_file, shopperstop_ean_col, crawled_file, crawled_barcode_col, output_dir):
    shopperstop_df = load_any(shopperstop_file)
    crawled_df = load_any(crawled_file)

    print(f"Shoppers Stop master: {len(shopperstop_df)} rows")
    print(f"Crawled catalog:      {len(crawled_df)} rows")

    if shopperstop_ean_col not in shopperstop_df.columns:
        raise ValueError(f"Column '{shopperstop_ean_col}' not found in {shopperstop_file}. "
                          f"Available columns: {list(shopperstop_df.columns)}")
    if crawled_barcode_col not in crawled_df.columns:
        raise ValueError(f"Column '{crawled_barcode_col}' not found in {crawled_file}. "
                          f"Available columns: {list(crawled_df.columns)}")

    shopperstop_df["_ean_key"] = normalize_ean(shopperstop_df[shopperstop_ean_col])
    crawled_df["_ean_key"] = normalize_ean(crawled_df[crawled_barcode_col])

    missing_ss = shopperstop_df["_ean_key"].isna().sum()
    missing_crawled = crawled_df["_ean_key"].isna().sum()
    if missing_ss:
        print(f"  warning: {missing_ss} Shoppers Stop rows have no EAN")
    if missing_crawled:
        print(f"  warning: {missing_crawled} crawled rows have no barcode")

    matched = shopperstop_df.merge(
        crawled_df, on="_ean_key", how="inner", suffixes=("_shopperstop", "_crawled")
    )

    matched_eans = set(matched["_ean_key"].dropna())
    unmatched_shopperstop = shopperstop_df[~shopperstop_df["_ean_key"].isin(matched_eans)]
    unmatched_crawled = crawled_df[~crawled_df["_ean_key"].isin(matched_eans)]

    print(f"\nMatched:                {len(matched)} rows")
    print(f"Unmatched (Shoppers Stop side): {len(unmatched_shopperstop)} rows")
    print(f"Unmatched (crawled side):       {len(unmatched_crawled)} rows")

    for df in (matched, unmatched_shopperstop, unmatched_crawled):
        df.drop(columns=["_ean_key"], inplace=True, errors="ignore")

    os.makedirs(output_dir, exist_ok=True)
    matched_path = os.path.join(output_dir, "matched.xlsx")
    unmatched_ss_path = os.path.join(output_dir, "unmatched_shopperstop.xlsx")
    unmatched_crawled_path = os.path.join(output_dir, "unmatched_crawled.xlsx")

    matched.to_excel(matched_path, index=False)
    unmatched_shopperstop.to_excel(unmatched_ss_path, index=False)
    unmatched_crawled.to_excel(unmatched_crawled_path, index=False)

    print(f"\nSaved:\n  {matched_path}\n  {unmatched_ss_path}\n  {unmatched_crawled_path}")

    return {
        "matched_count": len(matched),
        "unmatched_shopperstop_count": len(unmatched_shopperstop),
        "unmatched_crawled_count": len(unmatched_crawled),
        "matched_path": matched_path,
        "unmatched_shopperstop_path": unmatched_ss_path,
        "unmatched_crawled_path": unmatched_crawled_path,
        "matched_df": matched,
    }


def main():
    parser = argparse.ArgumentParser(description="Match a crawled catalog against a Shoppers Stop SKU/EAN file")
    parser.add_argument("--shopperstop-file", required=True, help="Shoppers Stop master Excel/CSV file")
    parser.add_argument("--shopperstop-ean-col", default="Active EAN", help="EAN column name in the Shoppers Stop file")
    parser.add_argument("--crawled-file", required=True, help="Crawled catalog CSV (from shopify_catalog_downloader.py)")
    parser.add_argument("--crawled-barcode-col", default="barcode", help="Barcode column name in the crawled file")
    parser.add_argument("--output-dir", required=True, help="Directory to write matched/unmatched output files")
    args = parser.parse_args()

    match(
        args.shopperstop_file, args.shopperstop_ean_col,
        args.crawled_file, args.crawled_barcode_col,
        args.output_dir,
    )


if __name__ == "__main__":
    main()
