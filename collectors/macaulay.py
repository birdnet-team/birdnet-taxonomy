#!/usr/bin/env python3
"""
Discover Macaulay Library taxon codes for all species.

Maps each species to its ML taxon code:
  - Birds use eBird species codes (e.g. "eurblk1")
  - Non-birds use t-prefixed numeric IDs (e.g. "t-11032766")

Resolution cascade:
  1. eBird code from existing data (birds only, no API call)
  2. ML taxonomy API lookup by scientific name
  3. Wikidata P10794 (Macaulay Library taxon ID) via SPARQL
  4. GBIF synonym lookup → retry ML API with alternate names

Input:  raw_data/inat_data.json, raw_data/ebird_data.json, raw_data/wikidata_data.json
Output: raw_data/macaulay_data.json

Usage:
    python -m collectors.macaulay [--limit N] [--group NAME] [--dry-run] [--new-only]
"""

import argparse
import json
import urllib.parse
import urllib.request

from tqdm import tqdm

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT,
    load_canonical_species, load_json, save_json,
    setup_shutdown, is_shutting_down,
    RateLimiter,
)

INAT_FILE = RAW_DIR / "inat_data.json"
EBIRD_FILE = RAW_DIR / "ebird_data.json"
WIKIDATA_FILE = RAW_DIR / "wikidata_data.json"
OUTPUT_FILE = RAW_DIR / "macaulay_data.json"

ML_TAXONOMY_URL = "https://taxonomy.api.macaulaylibrary.org/ws5.0/taxonomy-all"
ML_API_KEY = "PUB5447877383"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
GBIF_MATCH_URL = "https://api.gbif.org/v1/species/match"
GBIF_SYNONYMS_URL = "https://api.gbif.org/v1/species/{key}/synonyms"

_SPARQL_BATCH = 200

_rate = RateLimiter(10)  # ML taxonomy API


# ---------------------------------------------------------------------------
# ML taxonomy API
# ---------------------------------------------------------------------------

def _ml_lookup(scientific_name: str) -> str | None:
    """Query ML taxonomy API for a species code by scientific name.

    Returns the ML taxon code (eBird code or t-XXXXX) or None.
    """
    _rate.acquire()
    params = urllib.parse.urlencode({
        "key": ML_API_KEY,
        "taxaLocale": "en-US",
        "sortByHasMedia": "true",
        "sortByCategory": "false",
        "q": scientific_name,
    })
    url = f"{ML_TAXONOMY_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    # Filter to species-level entries matching the exact scientific name
    name_lower = scientific_name.lower()
    for item in data:
        code_parts = item.get("code", "").split(",")
        if len(code_parts) != 2:
            continue
        code, rank = code_parts[0], code_parts[1]
        if rank != "species":
            continue
        # Name format: "English Name - Scientific Name"
        display = item.get("name", "")
        if name_lower in display.lower():
            return code
    return None


# ---------------------------------------------------------------------------
# Wikidata bulk fetch (P10794)
# ---------------------------------------------------------------------------

def _sparql_query(query: str) -> list[dict]:
    """Run a SPARQL query against Wikidata."""
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
            return json.loads(resp.read())["results"]["bindings"]
    except Exception as e:
        print(f"  WARNING: Wikidata query failed: {e}")
        return []


def _wikidata_ml_codes(species_names: list[str]) -> dict[str, str]:
    """Bulk-fetch ML taxon codes (P10794) from Wikidata via SPARQL."""
    results = {}
    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        rows = _sparql_query(
            f"SELECT ?taxonName ?mlId WHERE {{"
            f"  VALUES ?taxonName {{ {values} }}"
            f"  ?item wdt:P225 ?taxonName ."
            f"  ?item wdt:P10794 ?mlId ."
            f"}}"
        )
        for r in rows:
            sci = r["taxonName"]["value"]
            results[sci] = r["mlId"]["value"]
    return results


# ---------------------------------------------------------------------------
# GBIF synonym fallback
# ---------------------------------------------------------------------------

def _gbif_synonyms(scientific_name: str) -> list[str]:
    """Get alternate scientific names for a species via GBIF."""
    # Step 1: match to get GBIF key
    params = urllib.parse.urlencode({"name": scientific_name, "strict": "true"})
    url = f"{GBIF_MATCH_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            match = json.loads(resp.read())
    except Exception:
        return []

    key = match.get("usageKey")
    if not key:
        return []

    # Step 2: fetch synonyms
    url2 = GBIF_SYNONYMS_URL.format(key=key) + "?limit=50"
    req2 = urllib.request.Request(url2, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            syns = json.loads(resp2.read())
    except Exception:
        return []

    names = []
    for s in syns.get("results", []):
        raw = s.get("canonicalName") or s.get("species") or ""
        raw = raw.strip()
        if raw and raw != scientific_name and is_full_species_name(raw):
            names.append(raw)
    return list(dict.fromkeys(names))  # dedupe preserving order


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Discover Macaulay Library taxon codes for species"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--group", type=str, default="",
                        help="Only process this taxon group")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without fetching")
    parser.add_argument("--new-only", action="store_true",
                        help="Only species not yet in macaulay_data.json")
    args = parser.parse_args()

    setup_shutdown()
    cfg = load_config()
    ml_cfg = cfg.get("macaulay", {})

    global _rate, ML_API_KEY
    _rate = RateLimiter(ml_cfg.get("rps", 10))
    ML_API_KEY = ml_cfg.get("api_key", ML_API_KEY)

    print("Loading species list...")
    species = load_canonical_species(cfg, group=args.group)
    if not species:
        print("ERROR: No species found. Run collectors/inat.py and build taxonomy first.")
        raise SystemExit(1)

    ebird = load_json(EBIRD_FILE)
    wikidata = load_json(WIKIDATA_FILE)
    existing = load_json(OUTPUT_FILE)

    print(f"  {len(species)} species"
          + (f" (group: {args.group})" if args.group else ""))

    if args.new_only:
        species = {s: r for s, r in species.items() if s not in existing}
        print(f"  {len(species)} without ML code")

    if args.limit:
        species = dict(list(species.items())[:args.limit])
        print(f"  Limited to {len(species)}")

    if args.dry_run:
        already = sum(1 for s in species if s in existing)
        print(f"  Already resolved: {already}")
        print(f"  Would process: {len(species) - already}")
        return

    # ------------------------------------------------------------------
    # Phase 1: eBird codes for birds (instant, no API calls)
    # ------------------------------------------------------------------
    phase1_count = 0
    for sci, rec in species.items():
        if sci in existing:
            continue
        if rec.get("taxon_group") != "Aves":
            continue
        # Check ebird_data or wikidata for an eBird code
        code = ebird.get(sci, {}).get("ebird_code", "")
        if not code:
            code = wikidata.get(sci, {}).get("ebird_code", "")
        if code:
            existing[sci] = {"ml_taxon_code": code}
            phase1_count += 1

    if phase1_count:
        print(f"\nPhase 1: eBird codes → {phase1_count} birds resolved")
        save_json(existing, OUTPUT_FILE)

    # ------------------------------------------------------------------
    # Phase 2: ML taxonomy API for remaining species
    # ------------------------------------------------------------------
    remaining = [s for s in species if s not in existing]
    if remaining:
        print(f"\nPhase 2: ML taxonomy API ({len(remaining)} species)...")
        phase2_count = 0
        progress = tqdm(remaining, desc="  ML lookup", unit="sp")
        for sci in progress:
            if is_shutting_down():
                break
            code = _ml_lookup(sci)
            if code:
                existing[sci] = {"ml_taxon_code": code}
                phase2_count += 1
            if phase2_count % 500 == 0 and phase2_count:
                save_json(existing, OUTPUT_FILE)
        progress.close()
        print(f"  Resolved {phase2_count} via ML API")
        save_json(existing, OUTPUT_FILE)

    if is_shutting_down():
        _print_stats(existing, species)
        return

    # ------------------------------------------------------------------
    # Phase 3: Wikidata P10794 for remaining
    # ------------------------------------------------------------------
    remaining = [s for s in species if s not in existing]
    if remaining:
        print(f"\nPhase 3: Wikidata P10794 ({len(remaining)} species)...")
        wd_codes = _wikidata_ml_codes(remaining)
        phase3_count = 0
        for sci, code in wd_codes.items():
            if sci not in existing:
                existing[sci] = {"ml_taxon_code": code}
                phase3_count += 1
        print(f"  Resolved {phase3_count} via Wikidata")
        if phase3_count:
            save_json(existing, OUTPUT_FILE)

    if is_shutting_down():
        _print_stats(existing, species)
        return

    # ------------------------------------------------------------------
    # Phase 4: GBIF synonym fallback → retry ML API
    # ------------------------------------------------------------------
    remaining = [s for s in species if s not in existing]
    if remaining:
        print(f"\nPhase 4: GBIF synonym fallback ({len(remaining)} species)...")
        phase4_count = 0
        progress = tqdm(remaining, desc="  GBIF→ML", unit="sp")
        for sci in progress:
            if is_shutting_down():
                break
            synonyms = _gbif_synonyms(sci)
            for syn in synonyms:
                code = _ml_lookup(syn)
                if code:
                    existing[sci] = {
                        "ml_taxon_code": code,
                        "matched_name": syn,
                        "aliases": [syn],
                    }
                    phase4_count += 1
                    break
            else:
                # No synonym matched — store null to avoid re-processing
                existing[sci] = {"ml_taxon_code": None}
        progress.close()
        print(f"  Resolved {phase4_count} via GBIF synonyms")
        save_json(existing, OUTPUT_FILE)

    _print_stats(existing, species)


def _print_stats(existing: dict, target: dict):
    """Print summary statistics."""
    total = len(existing)
    resolved = sum(1 for v in existing.values()
                   if v.get("ml_taxon_code") is not None)
    unresolved = total - resolved
    target_resolved = sum(1 for s in target
                          if existing.get(s, {}).get("ml_taxon_code") is not None)
    print(f"\nDone! {total} species in {OUTPUT_FILE.name}")
    print(f"  Resolved:   {resolved}")
    print(f"  Unresolved: {unresolved}")
    if target:
        pct = 100 * target_resolved / len(target)
        print(f"  Target coverage: {target_resolved}/{len(target)} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
