#!/usr/bin/env python3
"""
Merge all raw data into a single species metadata file.

Reads taxonomy.json (which already contains common names, images, and
identifiers from iNat, eBird, AviList, and Wikidata) and enriches each
species with a single description following the priority:

    Claude > Wikipedia > eBird

Produces a streamlined JSON (and optionally CSV + zip) under dist/.

Input files (all in raw_data/):
  - taxonomy.json       (built by utils/taxonomy.py — single source of truth)
  - ebird_data.json     (descriptions — birds only)
  - wikipedia_data.json (summaries, locale extracts)
  - claude_data.json    (optional: descriptions + translations)

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
import os
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw_data"

TAXONOMY_FILE = RAW / "taxonomy.json"


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def build_master(taxonomy: dict, ebird: dict, wiki: dict,
                 claude: dict) -> list[dict]:
    """Build streamlined species records from taxonomy + raw data sources.

    Each record contains:
      - scientific_name, common_name, taxon_group
      - common_names: {locale: name, ...}  (all available translations)
      - description: best English description (Claude > Wikipedia > eBird)
      - image_url, image_author, image_license, image_source
      - inat_id, ebird_code, gbif_id, ncbi_id, avibase_id, birdlife_id
      - observations_count
    """
    records = []
    desc_sources: dict[str, int] = Counter()

    for sci_name, tax in taxonomy.items():
        # Description priority: Claude > Wikipedia > eBird
        cl = claude.get(sci_name, {})
        wp = wiki.get(sci_name, {})
        eb = ebird.get(sci_name, {})

        description = ""
        if cl.get("description_en"):
            description = cl["description_en"]
            desc_sources["claude"] += 1
        elif wp.get("extract"):
            description = wp["extract"]
            desc_sources["wikipedia"] += 1
        elif eb.get("description"):
            description = eb["description"]
            desc_sources["ebird"] += 1
        else:
            desc_sources["none"] += 1

        record = {
            "scientific_name": sci_name,
            "common_name": tax.get("preferred_common_name", ""),
            "taxon_group": tax.get("taxon_group", ""),
            "common_names": tax.get("common_names", {}),
            "description": description,
            "image_url": tax.get("image_url", ""),
            "image_author": tax.get("image_author", ""),
            "image_license": tax.get("image_license", ""),
            "image_source": tax.get("image_source", ""),
            "inat_id": tax.get("inat_id"),
            "ebird_code": tax.get("ebird_code", ""),
            "gbif_id": tax.get("gbif_id", ""),
            "ncbi_id": tax.get("ncbi_id", ""),
            "avibase_id": tax.get("avibase_id", ""),
            "birdlife_id": tax.get("birdlife_id", ""),
            "observations_count": tax.get("observations_count", 0),
        }
        records.append(record)

    # Sort by taxon group, then observations count descending
    group_order = {"Aves": 0, "Mammalia": 1, "Reptilia": 2,
                   "Amphibia": 3, "Insecta": 4}
    records.sort(key=lambda r: (
        group_order.get(r["taxon_group"], 99),
        -r["observations_count"],
    ))

    # Stats
    groups = Counter(r["taxon_group"] for r in records)
    n_locales = len(set(
        loc for r in records for loc in r.get("common_names", {}).keys()
    ))
    img_sources = Counter(r.get("image_source", "") or "none" for r in records)

    print(f"  {len(records)} species")
    for g, n in sorted(groups.items()):
        print(f"    {g}: {n}")

    print(f"\n  Common names: {n_locales} locales")

    print(f"\n  Descriptions:")
    for src, cnt in desc_sources.most_common():
        print(f"    {src}: {cnt}")

    print(f"\n  Images:")
    for src, cnt in img_sources.most_common():
        print(f"    {src}: {cnt}")

    return records


def records_to_csv(records: list[dict]) -> str:
    """Convert records to CSV string.

    Flattens common_names into separate columns for the most common locales
    (top 30 by coverage).
    """
    # Find the top locales by coverage
    locale_counts: Counter[str] = Counter()
    for r in records:
        locale_counts.update(r.get("common_names", {}).keys())
    top_locales = [loc for loc, _ in locale_counts.most_common(30)]

    base_cols = [
        "scientific_name", "common_name", "taxon_group",
        "description",
        "image_url", "image_author", "image_license", "image_source",
        "inat_id", "ebird_code", "gbif_id", "ncbi_id",
        "avibase_id", "birdlife_id", "observations_count",
    ]
    locale_cols = [f"common_name_{loc}" for loc in top_locales]
    fieldnames = base_cols + locale_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for rec in records:
        row = {k: rec.get(k, "") for k in base_cols}
        for loc in top_locales:
            row[f"common_name_{loc}"] = rec.get("common_names", {}).get(loc, "")
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

    out_dir = ROOT / ("dev" if args.dev else "dist")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading raw data...")
    taxonomy = load_json(TAXONOMY_FILE)
    ebird = load_json(RAW / "ebird_data.json")
    wiki = load_json(RAW / "wikipedia_data.json")
    claude = load_json(RAW / "claude_data.json")

    print(f"  Taxonomy:  {len(taxonomy):>8} species")
    print(f"  eBird:     {len(ebird):>8} species")
    print(f"  Wikipedia: {len(wiki):>8} species")
    print(f"  Claude:    {len(claude):>8} species")

    if not taxonomy:
        print("ERROR: No taxonomy.json found. Run utils/taxonomy.py first.")
        raise SystemExit(1)

    print("\nMerging...")
    records = build_master(taxonomy, ebird, wiki, claude)

    # Write JSON (atomic)
    json_path = out_dir / "species_metadata.json"
    tmp = json_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, json_path)
    json_size = json_path.stat().st_size
    print(f"\n  JSON: {json_path} ({json_size / 1024 / 1024:.1f} MB)")

    # Write CSV (atomic)
    csv_path = out_dir / "species_metadata.csv"
    csv_text = records_to_csv(records)
    tmp = csv_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(csv_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, csv_path)
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
