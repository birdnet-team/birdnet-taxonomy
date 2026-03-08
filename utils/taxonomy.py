#!/usr/bin/env python3
"""
Build a unified species taxonomy by cross-referencing iNaturalist and AviList.

Reads inat_data.json and AviList CSV, matches species across taxonomies,
and outputs a canonical taxonomy.json that downstream scripts (ebird.py,
wikipedia.py, merge.py) can use as their single source of truth.

Matching strategy for birds (in priority order):
  1. Direct scientific name match (iNat name == AviList name)
  2. Common name match (iNat English name == AviList Clements English name)
  3. Wikidata lookup (query eBird taxon ID via SPARQL, by sci name then iNat ID)
  4. iNat-only birds (no AviList/eBird match found)
  5. AviList-only birds (not in iNat — added with ebird_code, no inat_id)

Non-bird taxon groups (Mammalia, Insecta, etc.) pass through from iNat as-is.

Output: raw_data/taxonomy.json

Usage:
    python -m utils.taxonomy [--dry-run]
"""

import argparse
import csv
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from utils.config import load_config

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_UA = "BirdNET-SpeciesData/1.0 (taxonomy matching)"

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw_data"
INAT_FILE = RAW / "inat_data.json"
OUTPUT_FILE = RAW / "taxonomy.json"


def load_inat() -> dict:
    """Load iNaturalist species data."""
    if not INAT_FILE.exists():
        print(f"ERROR: {INAT_FILE.name} not found. Run utils/inat.py first.")
        raise SystemExit(1)
    with open(INAT_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_avilist(cfg: dict) -> list[dict]:
    """Load AviList CSV rows (species rank only)."""
    csv_name = cfg.get("avilist", {}).get("csv_file", "")
    csv_path = RAW / csv_name if csv_name else None
    if not csv_path or not csv_path.exists():
        print(f"WARNING: AviList CSV not found at {csv_path}. "
              "Birds will not have eBird codes. Run utils/avilist.py first.")
        return []

    rows = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("Taxon_rank") != "species":
                continue
            sci = (row.get("Scientific_name") or "").strip()
            code = (row.get("Species_code_Cornell_Lab") or "").strip()
            en_clements = (row.get("English_name_Clements_v2024") or "").strip()
            en_avilist = (row.get("English_name_AviList") or "").strip()
            if sci and code:
                rows.append({
                    "scientific_name": sci,
                    "ebird_code": code,
                    "common_name_clements": en_clements,
                    "common_name_avilist": en_avilist,
                })
    return rows


def _sparql_query(query: str) -> list[dict]:
    """Run a SPARQL query against the Wikidata endpoint."""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    req = urllib.request.Request(
        f"{WIKIDATA_SPARQL}?{params}",
        headers={"User-Agent": WIKIDATA_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["results"]["bindings"]
    except Exception as e:
        print(f"  WARNING: Wikidata query failed: {e}")
        return []


def query_wikidata(unmatched: list[tuple[str, int]]) -> dict[str, str]:
    """Query Wikidata for eBird taxon IDs of unmatched species.

    Args:
        unmatched: List of (scientific_name, inat_id) tuples.

    Returns:
        Dict mapping scientific_name -> ebird_code for species found in Wikidata.
    """
    if not unmatched:
        return {}

    results = {}

    # Pass A: query by scientific name (P225 -> P3444 eBird taxon ID)
    values = " ".join(f'"{sci}"' for sci, _ in unmatched)
    rows = _sparql_query(
        f"SELECT ?taxonName ?ebirdId WHERE {{"
        f"  VALUES ?taxonName {{ {values} }}"
        f"  ?item wdt:P225 ?taxonName ."
        f"  ?item wdt:P3444 ?ebirdId ."
        f"}}"
    )
    for r in rows:
        results[r["taxonName"]["value"]] = r["ebirdId"]["value"]

    # Pass B: remaining species — query by iNaturalist taxon ID (P3151 -> P3444)
    remaining = [(sci, iid) for sci, iid in unmatched
                 if sci not in results and iid is not None]
    if remaining:
        inat_values = " ".join(f'"{iid}"' for _, iid in remaining)
        iid_to_sci = {str(iid): sci for sci, iid in remaining}
        rows = _sparql_query(
            f"SELECT ?inatId ?ebirdId WHERE {{"
            f"  VALUES ?inatId {{ {inat_values} }}"
            f"  ?item wdt:P3151 ?inatId ."
            f"  ?item wdt:P3444 ?ebirdId ."
            f"}}"
        )
        for r in rows:
            iid = r["inatId"]["value"]
            sci = iid_to_sci.get(iid)
            if sci and sci not in results:
                results[sci] = r["ebirdId"]["value"]

    return results


def build_taxonomy(inat: dict, avilist_rows: list[dict]) -> dict:
    """Cross-reference iNat and AviList to build a unified taxonomy.

    Returns dict keyed by scientific_name with fields:
      - inat_id, taxon_group, iconic_taxon_name, preferred_common_name
      - common_names, wikipedia_url, image_url, image_attribution, image_license
      - observations_count
      - ebird_code (for birds, empty string for others)
      - match_source: "direct" | "common_name" | "wikidata" | "inat_only" | "avilist_only"
    """
    taxonomy = {}

    # Build AviList lookup dicts
    avi_by_sci = {}   # scientific_name -> row
    avi_by_en = {}    # lower(common_name) -> row
    for row in avilist_rows:
        avi_by_sci[row["scientific_name"]] = row
        for name in (row["common_name_clements"], row["common_name_avilist"]):
            if name:
                avi_by_en[name.lower()] = row

    matched_sci = set()  # Track which AviList rows we've used
    stats = {"direct": 0, "common_name": 0, "wikidata": 0, "inat_only": 0,
             "avilist_only": 0, "non_bird": 0}
    pending_unmatched = []  # Collect (sci_name, inat_id) for Wikidata lookup

    # Pass 1: Process all iNat species
    for sci_name, rec in inat.items():
        if rec.get("inat_id") is None:
            continue

        is_bird = rec.get("taxon_group") == "Aves"
        ebird_code = ""
        match_source = ""

        if is_bird:
            # Try direct scientific name match
            avi_row = avi_by_sci.get(sci_name)
            if avi_row:
                ebird_code = avi_row["ebird_code"]
                match_source = "direct"
                matched_sci.add(avi_row["scientific_name"])
                stats["direct"] += 1
            else:
                # Try common name match
                cn = (rec.get("preferred_common_name") or "").lower()
                avi_row = avi_by_en.get(cn) if cn else None
                if avi_row and avi_row["scientific_name"] not in matched_sci:
                    ebird_code = avi_row["ebird_code"]
                    match_source = "common_name"
                    matched_sci.add(avi_row["scientific_name"])
                    stats["common_name"] += 1
                else:
                    # Mark as pending — will try Wikidata next
                    match_source = "inat_only"
                    pending_unmatched.append((sci_name, rec["inat_id"]))
        else:
            match_source = "non_bird"
            stats["non_bird"] += 1

        taxonomy[sci_name] = {
            "inat_id": rec["inat_id"],
            "taxon_group": rec.get("taxon_group", ""),
            "iconic_taxon_name": rec.get("iconic_taxon_name", ""),
            "preferred_common_name": rec.get("preferred_common_name", ""),
            "common_names": rec.get("common_names", {}),
            "wikipedia_url": rec.get("wikipedia_url", ""),
            "image_url": rec.get("image_url", ""),
            "image_attribution": rec.get("image_attribution", ""),
            "image_license": rec.get("image_license", ""),
            "observations_count": rec.get("observations_count", 0),
            "ebird_code": ebird_code,
            "match_source": match_source,
        }

    # Pass 1b: Wikidata lookup for unmatched birds
    if pending_unmatched:
        print(f"  Querying Wikidata for {len(pending_unmatched)} unmatched birds...")
        wd_results = query_wikidata(pending_unmatched)
        for sci_name, _ in pending_unmatched:
            ebird_code = wd_results.get(sci_name, "")
            if ebird_code:
                taxonomy[sci_name]["ebird_code"] = ebird_code
                taxonomy[sci_name]["match_source"] = "wikidata"
                stats["wikidata"] += 1
            else:
                stats["inat_only"] += 1

    # Pass 2: Add AviList-only species (birds not in iNat)
    for row in avilist_rows:
        if row["scientific_name"] in matched_sci:
            continue
        if row["scientific_name"] in taxonomy:
            continue  # Already added from iNat

        sci = row["scientific_name"]
        en = row["common_name_clements"] or row["common_name_avilist"] or ""
        taxonomy[sci] = {
            "inat_id": None,
            "taxon_group": "Aves",
            "iconic_taxon_name": "Aves",
            "preferred_common_name": en,
            "common_names": {"en": en} if en else {},
            "wikipedia_url": "",
            "image_url": "",
            "image_attribution": "",
            "image_license": "",
            "observations_count": 0,
            "ebird_code": row["ebird_code"],
            "match_source": "avilist_only",
        }
        stats["avilist_only"] += 1

    return taxonomy, stats


def main():
    parser = argparse.ArgumentParser(
        description="Build unified taxonomy from iNat + AviList")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show stats without writing output")
    args = parser.parse_args()

    cfg = load_config()

    print("Loading sources...")
    inat = load_inat()
    avilist_rows = load_avilist(cfg)
    print(f"  iNaturalist: {len(inat)} species")
    print(f"  AviList:     {len(avilist_rows)} bird species")

    print("\nBuilding taxonomy...")
    taxonomy, stats = build_taxonomy(inat, avilist_rows)

    # Summary
    total_birds = (stats["direct"] + stats["common_name"] + stats["wikidata"]
                   + stats["inat_only"] + stats["avilist_only"])
    matched_birds = stats["direct"] + stats["common_name"] + stats["wikidata"]
    inat_birds = matched_birds + stats["inat_only"]
    print(f"\n  Total species: {len(taxonomy)}")
    print(f"  Birds:         {total_birds}")
    print(f"    Direct match:     {stats['direct']}")
    print(f"    Common name:      {stats['common_name']}")
    print(f"    Wikidata:         {stats['wikidata']}")
    print(f"    iNat only:        {stats['inat_only']} (no eBird code)")
    print(f"    AviList only:     {stats['avilist_only']} (no iNat ID)")
    print(f"    Match rate:       {matched_birds}/{inat_birds} "
          f"iNat birds ({100 * matched_birds / max(1, inat_birds):.1f}%)")
    print(f"  Non-birds:     {stats['non_bird']}")

    if args.dry_run:
        # Show some unmatched examples
        unmatched = [(k, v) for k, v in taxonomy.items()
                     if v["taxon_group"] == "Aves" and v["match_source"] == "inat_only"]
        if unmatched:
            unmatched.sort(key=lambda x: -x[1].get("observations_count", 0))
            print(f"\n  Top unmatched iNat birds (by observations):")
            for sci, rec in unmatched[:15]:
                cn = rec.get("preferred_common_name", "")
                obs = rec.get("observations_count", 0)
                print(f"    {sci} — {cn} ({obs:,} obs)")
        return

    # Write output
    RAW.mkdir(exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(taxonomy, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUTPUT_FILE)
    size_mb = OUTPUT_FILE.stat().st_size / 1024 / 1024
    print(f"\n  Written: {OUTPUT_FILE} ({size_mb:.1f} MB)")
    print("Done!")


if __name__ == "__main__":
    main()
