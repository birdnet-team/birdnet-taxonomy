#!/usr/bin/env python3
"""
Merge all raw data into a single species metadata file.

Reads the intermediate outputs from earlier pipeline steps and joins them
into one record per species. Produces both JSON and CSV, compressed into
a zip archive under dist/.

This is intended to run as a release step — all raw data should already
be collected before running this script.

Input files (all in raw_data/):
  - inat_data.json     (step 2: taxonomy, common names, photos)
  - ebird_data.json    (step 3: descriptions, images — birds only)
  - wikipedia_data.json(step 4: summaries, locale URLs)
  - claude_data.json   (step 5: descriptions + translations)

Output:
  - dist/species_metadata.json
  - dist/species_metadata.csv
  - dist/species_metadata.zip  (contains both)

Usage:
    python merge.py [--dev] [--no-zip]
"""

import argparse
import csv
import io
import json
import zipfile
from pathlib import Path

from utils.config import load_config, get_locales

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw_data"
DIST = ROOT / "dist"


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def build_master(inat: dict, ebird: dict, wiki: dict, claude: dict,
                 locales: list[str]) -> list[dict]:
    """Join all sources into a flat list of species records."""
    records = []

    for sci_name, inat_rec in inat.items():
        if inat_rec.get("inat_id") is None:
            continue

        # Common names from iNat (keyed by locale)
        common_names = inat_rec.get("common_names", {})

        # eBird data (birds only)
        eb = ebird.get(sci_name, {})
        ebird_code = eb.get("ebird_code", "")
        ebird_desc = eb.get("description", "") or ""
        ebird_image = eb.get("image_url", "") or ""
        ebird_image_attr = eb.get("image_attribution", "") or ""

        # Wikipedia data
        wp = wiki.get(sci_name, {})
        wiki_extract = wp.get("extract", "") or ""
        wiki_urls = wp.get("wikipedia_urls", {})

        # Claude descriptions
        cl = claude.get(sci_name, {})
        desc_en = cl.get("description_en", "") or ""
        translations = cl.get("translations", {})

        # Build per-locale description dict (fall back to Wikipedia extract)
        descriptions = {}
        descriptions["en"] = desc_en if desc_en else wiki_extract
        for loc in locales:
            if loc != "en" and loc in translations:
                descriptions[loc] = translations[loc]

        record = {
            "scientific_name": sci_name,
            "common_name": inat_rec.get("preferred_common_name", ""),
            "taxon_group": inat_rec.get("taxon_group", ""),
            "iconic_taxon_name": inat_rec.get("iconic_taxon_name", ""),
            "inat_id": inat_rec.get("inat_id"),
            "observations_count": inat_rec.get("observations_count", 0),
            "common_names": common_names,
            "image_url": inat_rec.get("image_url", ""),
            "image_attribution": inat_rec.get("image_attribution", ""),
            "image_license": inat_rec.get("image_license", ""),
            "ebird_code": ebird_code,
            "ebird_description": ebird_desc,
            "ebird_image_url": ebird_image,
            "ebird_image_attribution": ebird_image_attr,
            "wikipedia_extract": wiki_extract,
            "wikipedia_urls": wiki_urls,
            "descriptions": descriptions,
        }
        records.append(record)

    # Sort by taxon group, then observations count descending
    group_order = {"Aves": 0, "Mammalia": 1, "Reptilia": 2,
                   "Amphibia": 3, "Insecta": 4}
    records.sort(key=lambda r: (
        group_order.get(r["taxon_group"], 99),
        -r["observations_count"],
    ))

    return records


def records_to_csv(records: list[dict], locales: list[str]) -> str:
    """Convert records to CSV string.

    Flattens nested dicts: common_names become columns like
    common_name_en, common_name_de, etc.  wikipedia_urls become
    wikipedia_url_en, etc.  descriptions become description_en, etc.
    """
    # Build column list
    base_cols = [
        "scientific_name", "common_name", "taxon_group",
        "iconic_taxon_name", "inat_id", "observations_count",
        "image_url", "image_attribution", "image_license",
        "ebird_code", "ebird_description",
        "ebird_image_url", "ebird_image_attribution",
        "wikipedia_extract",
    ]
    locale_cols = []
    for loc in locales:
        locale_cols.append(f"common_name_{loc}")
    for loc in locales:
        locale_cols.append(f"wikipedia_url_{loc}")
    for loc in locales:
        locale_cols.append(f"description_{loc}")

    fieldnames = base_cols + locale_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for rec in records:
        row = {k: rec.get(k, "") for k in base_cols}
        for loc in locales:
            row[f"common_name_{loc}"] = rec.get("common_names", {}).get(loc, "")
            row[f"wikipedia_url_{loc}"] = rec.get("wikipedia_urls", {}).get(loc, "")
            row[f"description_{loc}"] = rec.get("descriptions", {}).get(loc, "")
        writer.writerow(row)

    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(
        description="Merge raw data into species_metadata.json/csv"
    )
    parser.add_argument("--dev", action="store_true",
                        help="Write to dev/ instead of dist/")
    parser.add_argument("--no-zip", action="store_true",
                        help="Write uncompressed files only, skip zip")
    args = parser.parse_args()

    cfg = load_config()
    locales = get_locales()
    out_dir = ROOT / ("dev" if args.dev else "dist")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading raw data...")
    inat = load_json(RAW / "inat_data.json")
    ebird = load_json(RAW / "ebird_data.json")
    wiki = load_json(RAW / "wikipedia_data.json")
    claude = load_json(RAW / "claude_data.json")

    print(f"  iNat:      {len(inat):>8} species")
    print(f"  eBird:     {len(ebird):>8} species")
    print(f"  Wikipedia: {len(wiki):>8} species")
    print(f"  Claude:    {len(claude):>8} species")

    if not inat:
        print("ERROR: No iNat data found. Run the pipeline first.")
        raise SystemExit(1)

    print("\nMerging...")
    records = build_master(inat, ebird, wiki, claude, locales)
    print(f"  {len(records)} species in master taxonomy")

    # Group stats
    from collections import Counter
    groups = Counter(r["taxon_group"] for r in records)
    for g, n in sorted(groups.items()):
        print(f"    {g}: {n}")

    # Write JSON
    json_path = out_dir / "species_metadata.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    json_size = json_path.stat().st_size
    print(f"\n  JSON: {json_path} ({json_size / 1024 / 1024:.1f} MB)")

    # Write CSV
    csv_path = out_dir / "species_metadata.csv"
    csv_text = records_to_csv(records, locales)
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(csv_text)
    csv_size = csv_path.stat().st_size
    print(f"  CSV:  {csv_path} ({csv_size / 1024 / 1024:.1f} MB)")

    # Zip
    if not args.no_zip:
        zip_path = out_dir / "species_metadata.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(json_path, "species_metadata.json")
            zf.write(csv_path, "species_metadata.csv")
        zip_size = zip_path.stat().st_size
        print(f"  ZIP:  {zip_path} ({zip_size / 1024 / 1024:.1f} MB)")

    print("\nDone!")


if __name__ == "__main__":
    main()
