#!/usr/bin/env python3
"""
Download and convert the AviList checklist.

Downloads the AviList XLSX from the official URL (configured in config.yml)
and converts it to CSV for use by the rest of the pipeline.

Output: raw_data/AviList-v2025-11Jun-extended.csv

Usage:
    python -m collectors.avilist [--force]

Requires: openpyxl (pip install openpyxl)
"""

import argparse
import csv
import sys
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from config import load_config
from collectors._common import ROOT, RAW_DIR, USER_AGENT


def download_xlsx(url: str) -> bytes:
    """Download the AviList XLSX file."""
    print(f"  Downloading {url}...")
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"  ERROR: {e}")
        raise SystemExit(1)
    print(f"  Downloaded {len(data) / 1024 / 1024:.1f} MB")
    return data


def xlsx_to_csv(xlsx_bytes: bytes, csv_path: Path):
    """Convert XLSX to semicolon-delimited CSV."""
    try:
        import openpyxl
    except ImportError:
        print("ERROR: openpyxl is required. Install with: pip install openpyxl")
        raise SystemExit(1)

    print("  Converting XLSX to CSV...")
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb.active

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        row_count = 0
        for row in ws.iter_rows(values_only=True):
            writer.writerow([cell if cell is not None else "" for cell in row])
            row_count += 1

    wb.close()
    print(f"  Wrote {row_count} rows to {csv_path.name}")


def main():
    cfg = load_config()
    avilist_cfg = cfg.get("avilist", {})
    url = avilist_cfg.get("url", "")
    csv_name = avilist_cfg.get("csv_file", "")

    if not url or not csv_name:
        print("ERROR: avilist.url and avilist.csv_file must be set in config.yml")
        raise SystemExit(1)

    csv_path = RAW_DIR / csv_name

    parser = argparse.ArgumentParser(description="Download and convert AviList checklist")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if CSV already exists")
    args = parser.parse_args()

    if csv_path.exists() and not args.force:
        print(f"AviList CSV already exists: {csv_path.name}")
        print("  Use --force to re-download.")
        return

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    xlsx_data = download_xlsx(url)

    # Also save the xlsx for reference
    xlsx_name = url.rsplit("/", 1)[-1]
    xlsx_path = RAW_DIR / xlsx_name
    xlsx_path.write_bytes(xlsx_data)
    print(f"  Saved XLSX as {xlsx_path.name}")

    xlsx_to_csv(xlsx_data, csv_path)
    print("Done!")


if __name__ == "__main__":
    main()
