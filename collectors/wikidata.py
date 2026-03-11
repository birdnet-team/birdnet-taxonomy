#!/usr/bin/env python3
"""
Fetch species identifiers, common name labels, and images from Wikidata
and Wikimedia Commons.

Queries Wikidata SPARQL for:
  - eBird species codes (P3444) via scientific name (P225) or iNat ID (P3151)
  - External identifiers: GBIF (P846), NCBI (P685), Avibase (P2426),
    BirdLife (P5257)
  - Common name labels (rdfs:label) in all available languages
  - P18 images → Wikimedia Commons license checks

Input:
  - raw_data/inat_data.json  (species list with iNat IDs)
  - AviList CSV              (bird species list)

Output: raw_data/wikidata_data.json

Usage:
    python -m collectors.wikidata [--no-cache] [--dry-run]
"""

import argparse
import csv
import hashlib
import json
import re
import urllib.parse
import urllib.request

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT, load_json, save_json,
    cache_key, cache_get, cache_put,
)

INAT_FILE = RAW_DIR / "inat_data.json"
OUTPUT_FILE = RAW_DIR / "wikidata_data.json"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"

_SPARQL_BATCH = 150
_COMMONS_BATCH = 50

_use_cache = True

# Wikidata properties for species identifiers
WD_IDENTIFIERS = {
    "ebird_code": "P3444",
    "gbif_id": "P846",
    "ncbi_id": "P685",
    "avibase_id": "P2426",
    "birdlife_id": "P5257",
}

# Normalize locale codes across sources to canonical forms.
LOCALE_NORMALIZE: dict[str, str] = {
    "nb": "no",
    "pt-br": "pt",
}


# ---------------------------------------------------------------------------
# SPARQL helpers
# ---------------------------------------------------------------------------

def _sparql_query(query: str) -> list[dict]:
    """Run a SPARQL query against Wikidata (POST to avoid URL limits)."""
    if _use_cache:
        key = cache_key("sparql", query)
        cached = cache_get(key)
        if cached is not None:
            return cached
    else:
        key = None

    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        WIKIDATA_SPARQL, data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())["results"]["bindings"]
    except Exception as e:
        print(f"  WARNING: Wikidata query failed: {e}")
        return []

    if key:
        cache_put(key, result)
    return result


# ---------------------------------------------------------------------------
# Phase 1: eBird codes for unmatched species
# ---------------------------------------------------------------------------

def query_ebird_codes(
    species: list[tuple[str, int | None]],
) -> dict[str, str]:
    """Query Wikidata for eBird taxon IDs.

    Args:
        species: list of (scientific_name, inat_id) tuples.

    Returns {scientific_name: ebird_code}.
    """
    if not species:
        return {}

    results = {}

    # Pass A: by scientific name (P225 → P3444)
    for i in range(0, len(species), _SPARQL_BATCH):
        batch = species[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{sci}"' for sci, _ in batch)
        rows = _sparql_query(
            f"SELECT ?taxonName ?ebirdId WHERE {{"
            f"  VALUES ?taxonName {{ {values} }}"
            f"  ?item wdt:P225 ?taxonName ."
            f"  ?item wdt:P3444 ?ebirdId ."
            f"}}"
        )
        for r in rows:
            results[r["taxonName"]["value"]] = r["ebirdId"]["value"]

    # Pass B: remaining — by iNaturalist taxon ID (P3151 → P3444)
    remaining = [(sci, iid) for sci, iid in species
                 if sci not in results and iid is not None]
    if remaining:
        iid_to_sci = {str(iid): sci for sci, iid in remaining}
        for i in range(0, len(remaining), _SPARQL_BATCH):
            batch = remaining[i:i + _SPARQL_BATCH]
            inat_values = " ".join(f'"{iid}"' for _, iid in batch)
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


# ---------------------------------------------------------------------------
# Phase 2: External identifiers (GBIF, NCBI, Avibase, BirdLife)
# ---------------------------------------------------------------------------

def query_identifiers(species_names: list[str]) -> dict[str, dict]:
    """Batch-query Wikidata for external identifiers."""
    if not species_names:
        return {}

    optionals = ""
    selects = ["?taxonName"]
    for key, prop in WD_IDENTIFIERS.items():
        if key == "ebird_code":
            continue
        var = f"?{key}"
        selects.append(var)
        optionals += f"  OPTIONAL {{ ?item wdt:{prop} {var} . }}\n"

    results = {}
    select_str = " ".join(selects)

    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        query = (
            f"SELECT {select_str} WHERE {{\n"
            f"  VALUES ?taxonName {{ {values} }}\n"
            f"  ?item wdt:P225 ?taxonName .\n"
            f"{optionals}}}"
        )
        rows = _sparql_query(query)
        for r in rows:
            sci = r["taxonName"]["value"]
            ids = {}
            for key in WD_IDENTIFIERS:
                if key == "ebird_code":
                    continue
                val = r.get(key, {}).get("value", "")
                if val:
                    ids[key] = val
            if ids:
                results[sci] = ids

    return results


# ---------------------------------------------------------------------------
# Phase 3: Common name labels (all languages)
# ---------------------------------------------------------------------------

def query_labels(species_names: list[str]) -> dict[str, dict[str, str]]:
    """Query Wikidata for species labels in ALL languages."""
    if not species_names:
        return {}

    results: dict[str, dict[str, str]] = {}

    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        query = (
            f"SELECT ?taxonName ?label WHERE {{\n"
            f"  VALUES ?taxonName {{ {values} }}\n"
            f"  ?item wdt:P225 ?taxonName .\n"
            f"  ?item rdfs:label ?label .\n"
            f"  FILTER(STRLEN(LANG(?label)) >= 2)\n"
            f"}}"
        )
        rows = _sparql_query(query)
        for r in rows:
            sci = r["taxonName"]["value"]
            label = r["label"]["value"]
            lang = r["label"].get("xml:lang", "")
            canonical = LOCALE_NORMALIZE.get(lang, lang)
            if label == sci:
                continue
            if canonical not in results.get(sci, {}):
                results.setdefault(sci, {})[canonical] = label

    return results


# ---------------------------------------------------------------------------
# Phase 4: Wikidata P18 images + Commons license check
# ---------------------------------------------------------------------------

def _commons_url(filename: str) -> str:
    """Build a Wikimedia Commons direct URL from a filename."""
    filename = filename.replace(" ", "_")
    md5 = hashlib.md5(filename.encode("utf-8")).hexdigest()
    encoded = urllib.parse.quote(filename, safe="")
    return (f"https://upload.wikimedia.org/wikipedia/commons/"
            f"{md5[0]}/{md5[:2]}/{encoded}")


def _is_commons_license_ok(license_short: str) -> bool:
    if not license_short:
        return False
    low = license_short.lower()
    return any(x in low for x in ("cc", "public domain", "pd", "gfdl"))


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def query_images(species_names: list[str]) -> dict[str, str]:
    """Query Wikidata P18 (image) → {scientific_name: Commons filename}."""
    if not species_names:
        return {}

    results: dict[str, str] = {}
    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        rows = _sparql_query(
            f"SELECT ?taxonName ?image WHERE {{\n"
            f"  VALUES ?taxonName {{ {values} }}\n"
            f"  ?item wdt:P225 ?taxonName .\n"
            f"  ?item wdt:P18 ?image .\n"
            f"}}"
        )
        for r in rows:
            sci = r["taxonName"]["value"]
            image_url = r["image"]["value"]
            filename = urllib.parse.unquote(image_url.split("/")[-1])
            if sci not in results:
                results[sci] = filename

    return results


def check_commons_licenses(
    filenames: dict[str, str],
) -> dict[str, dict]:
    """Batch-check licenses for Wikimedia Commons files.

    Args:
        filenames: {scientific_name: commons_filename}

    Returns {scientific_name: {url, attribution, license, license_url}}.
    """
    if not filenames:
        return {}

    file_to_sci: dict[str, list[str]] = {}
    for sci, fn in filenames.items():
        norm = fn.replace(" ", "_")
        file_to_sci.setdefault(norm, []).append(sci)

    all_files = list(file_to_sci.keys())
    results: dict[str, dict] = {}

    for i in range(0, len(all_files), _COMMONS_BATCH):
        batch = all_files[i:i + _COMMONS_BATCH]
        titles = "|".join(f"File:{fn}" for fn in batch)

        if _use_cache:
            key = cache_key("commons", titles)
            cached_data = cache_get(key)
        else:
            key = None
            cached_data = None

        if cached_data is not None:
            data = cached_data
        else:
            post_data = urllib.parse.urlencode({
                "action": "query",
                "titles": titles,
                "prop": "imageinfo",
                "iiprop": "extmetadata",
                "format": "json",
            }).encode("utf-8")
            req = urllib.request.Request(
                _COMMONS_API, data=post_data,
                headers={"User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                print(f"  WARNING: Commons license check failed: {e}")
                continue
            if key:
                cache_put(key, data)

        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            title = page.get("title", "")
            if not title.startswith("File:"):
                continue
            fn = title[5:]

            imageinfo = page.get("imageinfo", [])
            if not imageinfo:
                continue
            meta = imageinfo[0].get("extmetadata", {})

            license_short = meta.get("LicenseShortName", {}).get("value", "")
            license_url = meta.get("LicenseUrl", {}).get("value", "")
            artist_html = meta.get("Artist", {}).get("value", "")
            artist = _strip_html_tags(artist_html) if artist_html else ""

            if not _is_commons_license_ok(license_short):
                continue

            url = _commons_url(fn)
            norm = fn.replace(" ", "_")
            for sci in file_to_sci.get(norm, []):
                results[sci] = {
                    "url": url,
                    "attribution": artist,
                    "license": license_short,
                    "license_url": license_url,
                }

    return results


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------

def _load_species_list(cfg: dict) -> dict[str, int | None]:
    """Load all species from inat_data.json + AviList CSV.

    Returns {scientific_name: inat_id_or_None}.
    """
    species: dict[str, int | None] = {}

    # From iNaturalist
    if INAT_FILE.exists():
        inat = load_json(INAT_FILE)
        for sci, rec in inat.items():
            inat_id = rec.get("inat_id")
            if inat_id is not None:
                species[sci] = inat_id

    # From AviList CSV (adds species not in iNat)
    csv_name = cfg.get("avilist", {}).get("csv_file", "")
    csv_path = RAW_DIR / csv_name if csv_name else None
    if csv_path and csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                if row.get("Taxon_rank") != "species":
                    continue
                sci = (row.get("Scientific_name") or "").strip()
                if sci and sci not in species:
                    species[sci] = None

    return species


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch species data from Wikidata and Wikimedia Commons"
    )
    parser.add_argument("--no-cache", action="store_true",
                        help="Bypass request cache (re-fetch all remote data)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show species count without querying")
    args = parser.parse_args()

    global _use_cache
    if args.no_cache:
        _use_cache = False

    cfg = load_config()

    print("Loading species list...")
    species = _load_species_list(cfg)
    if not species:
        print("ERROR: No species found. Run collectors/inat.py and/or "
              "collectors/avilist.py first.")
        raise SystemExit(1)
    print(f"  {len(species)} species")

    if args.dry_run:
        from_inat = sum(1 for v in species.values() if v is not None)
        avilist_only = len(species) - from_inat
        print(f"  From iNat: {from_inat}")
        print(f"  AviList only: {avilist_only}")
        return

    all_names = list(species.keys())
    existing = load_json(OUTPUT_FILE)

    # Phase 1: eBird codes
    # Only query for species that don't already have an eBird code
    need_ebird = [
        (sci, species[sci])
        for sci in all_names
        if not existing.get(sci, {}).get("ebird_code")
    ]
    n_batches = (len(need_ebird) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
    print(f"\nPhase 1: eBird codes ({len(need_ebird)} species, "
          f"{n_batches} batches)...")
    ebird_codes = query_ebird_codes(need_ebird)
    print(f"  Found {len(ebird_codes)} eBird codes")

    for sci, code in ebird_codes.items():
        existing.setdefault(sci, {})["ebird_code"] = code

    # Phase 2: External identifiers
    need_ids = [sci for sci in all_names
                if not existing.get(sci, {}).get("gbif_id")]
    n_batches = (len(need_ids) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
    print(f"\nPhase 2: Identifiers ({len(need_ids)} species, "
          f"{n_batches} batches)...")
    identifiers = query_identifiers(need_ids)
    print(f"  Found identifiers for {len(identifiers)} species")

    for sci, ids in identifiers.items():
        entry = existing.setdefault(sci, {})
        for key, val in ids.items():
            entry[key] = val

    save_json(existing, OUTPUT_FILE)

    # Phase 3: Labels (all languages)
    need_labels = [sci for sci in all_names
                   if not existing.get(sci, {}).get("labels")]
    n_batches = (len(need_labels) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
    print(f"\nPhase 3: Labels ({len(need_labels)} species, "
          f"{n_batches} batches)...")
    labels = query_labels(need_labels)
    print(f"  Found labels for {len(labels)} species")

    for sci, lbls in labels.items():
        existing.setdefault(sci, {})["labels"] = lbls

    save_json(existing, OUTPUT_FILE)

    # Phase 4: Images (P18 + Commons license check)
    need_image = [sci for sci in all_names
                  if not existing.get(sci, {}).get("image")]
    n_batches = (len(need_image) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
    print(f"\nPhase 4: Images ({len(need_image)} species, "
          f"{n_batches} batches)...")
    wd_images = query_images(need_image)
    print(f"  Found {len(wd_images)} P18 images, checking Commons licenses...")
    commons_results = check_commons_licenses(wd_images)
    print(f"  {len(commons_results)} with acceptable licenses")

    for sci, info in commons_results.items():
        existing.setdefault(sci, {})["image"] = info

    save_json(existing, OUTPUT_FILE)

    # Stats
    total = len(existing)
    has_ebird = sum(1 for v in existing.values() if v.get("ebird_code"))
    has_ids = sum(1 for v in existing.values() if v.get("gbif_id"))
    has_labels = sum(1 for v in existing.values() if v.get("labels"))
    has_image = sum(1 for v in existing.values() if v.get("image"))
    print(f"\nDone! {total} species in {OUTPUT_FILE.name}")
    print(f"  eBird codes:  {has_ebird}")
    print(f"  Identifiers:  {has_ids}")
    print(f"  Labels:       {has_labels}")
    print(f"  Images:       {has_image}")


if __name__ == "__main__":
    main()
