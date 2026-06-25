#!/usr/bin/env python3
"""
Map species to their Xeno-Canto scientific names.

XC uses IOC taxonomy which may differ from our iNat/eBird-based names.
This collector finds the XC-equivalent name for each species.

Resolution cascade:
  1. Wikidata P2426 (XC species ID) via bulk SPARQL — covers ~31k species
  2. XC API v3 direct lookup by genus + epithet
  3. XC API epithet-only search with group filter (catches genus transfers)
  4. GBIF synonym lookup → retry XC API with alternate names
  5. XC API English name search (final fallback)

Input:  raw_data/inat_data.json
Output: raw_data/xc_data.json

Usage:
    python -m collectors.xenocanto [--limit N] [--group NAME] [--dry-run] [--new-only]
"""

import argparse
import json
import urllib.parse
import urllib.request

from tqdm import tqdm

from config import load_config, load_env_value
from collectors._common import (
    RAW_DIR, USER_AGENT,
    is_full_species_name, load_canonical_species, load_json, save_json,
    setup_shutdown, is_shutting_down,
    RateLimiter,
)

INAT_FILE = RAW_DIR / "inat_data.json"
OUTPUT_FILE = RAW_DIR / "xc_data.json"

XC_API_URL = "https://xeno-canto.org/api/3/recordings"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
GBIF_MATCH_URL = "https://api.gbif.org/v1/species/match"
GBIF_SYNONYMS_URL = "https://api.gbif.org/v1/species/{key}/synonyms"

_SPARQL_BATCH = 200

# Map iNat iconic taxon names to XC group tags
_INAT_TO_XC_GROUP = {
    "Aves": "birds",
    "Mammalia": "land mammals",
    "Insecta": "grasshoppers",
    "Amphibia": "frogs",
    "Reptilia": "",  # XC has no reptile group tag
}

_rate = RateLimiter(5)  # XC API


def _get_xc_key() -> str:
    """Load XC API key from .env or environment."""
    key = load_env_value("XC_API_KEY")
    if not key:
        print("ERROR: XC_API_KEY not found in .env or environment.")
        print("  Get your key at https://xeno-canto.org/account")
        raise SystemExit(1)
    return key


# ---------------------------------------------------------------------------
# XC API helpers
# ---------------------------------------------------------------------------

def _xc_search(query: str, api_key: str) -> dict | None:
    """Query XC API v3. Returns response dict or None on error."""
    _rate.acquire()
    params = urllib.parse.urlencode({
        "query": query,
        "key": api_key,
        "per_page": "50",
    })
    url = f"{XC_API_URL}?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _xc_name_from_response(data: dict) -> str | None:
    """Extract the XC scientific name from a search response."""
    n = int(data.get("numRecordings", "0"))
    if n == 0:
        return None
    recs = data.get("recordings", [])
    if not recs:
        return None
    r = recs[0]
    gen = (r.get("gen") or "").strip()
    sp = (r.get("sp") or "").strip()
    if gen and sp:
        return f"{gen} {sp}"
    return None


def _xc_lookup_direct(scientific_name: str, api_key: str) -> str | None:
    """Search XC by gen:X+sp:Y."""
    parts = scientific_name.split()
    if len(parts) != 2:
        return None
    gen, sp = parts
    data = _xc_search(f"gen:{gen}+sp:{sp}", api_key)
    if data:
        return _xc_name_from_response(data)
    return None


def _xc_lookup_epithet(scientific_name: str, xc_group: str,
                       api_key: str) -> str | None:
    """Search XC by epithet + group (catches genus transfers)."""
    parts = scientific_name.split()
    if len(parts) != 2:
        return None
    epithet = parts[1]
    query = f"sp:{epithet}"
    if xc_group:
        query += f'+grp:"{xc_group}"'
    data = _xc_search(query, api_key)
    if not data:
        return None
    # May return multiple species — find the one with matching epithet
    for r in data.get("recordings", []):
        r_sp = (r.get("sp") or "").strip()
        if r_sp == epithet:
            gen = (r.get("gen") or "").strip()
            if gen:
                return f"{gen} {epithet}"
    return None


def _xc_lookup_english(common_name: str, api_key: str) -> str | None:
    """Search XC by English name (exact match)."""
    if not common_name:
        return None
    data = _xc_search(f'en:"={common_name}"', api_key)
    if data:
        return _xc_name_from_response(data)
    return None


# ---------------------------------------------------------------------------
# Wikidata bulk fetch (P2426 = XC species ID)
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


def _wikidata_xc_names(species_names: list[str]) -> dict[str, str]:
    """Bulk-fetch XC species IDs (P2426) from Wikidata.

    P2426 values are in 'Genus-epithet' format. We convert to 'Genus epithet'.
    """
    results = {}
    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        rows = _sparql_query(
            f"SELECT ?taxonName ?xcId WHERE {{"
            f"  VALUES ?taxonName {{ {values} }}"
            f"  ?item wdt:P225 ?taxonName ."
            f"  ?item wdt:P2426 ?xcId ."
            f"}}"
        )
        for r in rows:
            sci = r["taxonName"]["value"]
            xc_id = r["xcId"]["value"]
            # Convert "Genus-epithet" → "Genus epithet"
            xc_name = xc_id.replace("-", " ").strip()
            if is_full_species_name(xc_name):
                results[sci] = xc_name
    return results


# ---------------------------------------------------------------------------
# GBIF synonym fallback
# ---------------------------------------------------------------------------

def _gbif_synonyms(scientific_name: str) -> list[str]:
    """Get alternate scientific names via GBIF."""
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
    return list(dict.fromkeys(names))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Map species to Xeno-Canto scientific names"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--group", type=str, default="",
                        help="Only process this taxon group")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without fetching")
    parser.add_argument("--new-only", action="store_true",
                        help="Only species not yet in xc_data.json")
    parser.add_argument("--retry-unresolved", action="store_true",
                        help="Retry species cached with no XC mapping")
    args = parser.parse_args()

    setup_shutdown()
    cfg = load_config()
    xc_cfg = cfg.get("xenocanto", {})

    global _rate
    _rate = RateLimiter(xc_cfg.get("rps", 5))

    api_key = _get_xc_key()

    print("Loading species list...")
    species = load_canonical_species(cfg, group=args.group)
    if not species:
        print("ERROR: No species found. Run collectors.inat.py and build taxonomy first.")
        raise SystemExit(1)

    existing = load_json(OUTPUT_FILE)

    print(f"  {len(species)} species"
          + (f" (group: {args.group})" if args.group else ""))

    if args.new_only:
        species = {s: r for s, r in species.items() if s not in existing}
        print(f"  {len(species)} without XC mapping")

    if args.retry_unresolved:
        retry_names = {
            sci for sci in species
            if sci in existing and existing.get(sci, {}).get("xc_name") is None
        }
        for sci in retry_names:
            existing.pop(sci, None)
        print(f"  Retrying unresolved XC mappings: {len(retry_names)}")

    if args.limit:
        species = dict(list(species.items())[:args.limit])
        print(f"  Limited to {len(species)}")

    if args.dry_run:
        already = sum(1 for s in species if s in existing)
        print(f"  Already resolved: {already}")
        print(f"  Would process: {len(species) - already}")
        return

    # ------------------------------------------------------------------
    # Phase 1: Wikidata P2426 bulk fetch
    # ------------------------------------------------------------------
    need_wd = [s for s in species if s not in existing]
    if need_wd:
        n_batches = (len(need_wd) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
        print(f"\nPhase 1: Wikidata P2426 ({len(need_wd)} species, "
              f"{n_batches} batches)...")
        wd_names = _wikidata_xc_names(need_wd)
        phase1_count = 0
        for sci, xc_name in wd_names.items():
            if sci not in existing:
                existing[sci] = {"xc_name": xc_name}
                phase1_count += 1
        print(f"  Resolved {phase1_count} via Wikidata")
        if phase1_count:
            save_json(existing, OUTPUT_FILE)

    if is_shutting_down():
        _print_stats(existing, species)
        return

    # ------------------------------------------------------------------
    # Phase 2: XC API direct lookup (gen + sp)
    # ------------------------------------------------------------------
    remaining = [s for s in species if s not in existing]
    if remaining:
        print(f"\nPhase 2: XC API direct lookup ({len(remaining)} species)...")
        phase2_count = 0
        progress = tqdm(remaining, desc="  XC lookup", unit="sp")
        for sci in progress:
            if is_shutting_down():
                break
            xc_name = _xc_lookup_direct(sci, api_key)
            if xc_name:
                existing[sci] = {"xc_name": xc_name}
                phase2_count += 1
            if phase2_count % 200 == 0 and phase2_count:
                save_json(existing, OUTPUT_FILE)
        progress.close()
        print(f"  Resolved {phase2_count} via XC direct")
        save_json(existing, OUTPUT_FILE)

    if is_shutting_down():
        _print_stats(existing, species)
        return

    # ------------------------------------------------------------------
    # Phase 3: XC epithet search with group filter
    # ------------------------------------------------------------------
    remaining = [s for s in species if s not in existing]
    if remaining:
        print(f"\nPhase 3: XC epithet + group ({len(remaining)} species)...")
        phase3_count = 0
        progress = tqdm(remaining, desc="  XC epithet", unit="sp")
        for sci in progress:
            if is_shutting_down():
                break
            rec = species[sci]
            group = rec.get("taxon_group", "")
            xc_grp = _INAT_TO_XC_GROUP.get(group, "")
            xc_name = _xc_lookup_epithet(sci, xc_grp, api_key)
            if xc_name:
                existing[sci] = {"xc_name": xc_name}
                phase3_count += 1
        progress.close()
        print(f"  Resolved {phase3_count} via epithet search")
        if phase3_count:
            save_json(existing, OUTPUT_FILE)

    if is_shutting_down():
        _print_stats(existing, species)
        return

    # ------------------------------------------------------------------
    # Phase 4: GBIF synonyms → retry XC API
    # ------------------------------------------------------------------
    remaining = [s for s in species if s not in existing]
    if remaining:
        print(f"\nPhase 4: GBIF synonym fallback ({len(remaining)} species)...")
        phase4_count = 0
        progress = tqdm(remaining, desc="  GBIF→XC", unit="sp")
        for sci in progress:
            if is_shutting_down():
                break
            rec = species[sci]
            synonyms = [
                *rec.get("scientific_name_aliases", []),
                *_gbif_synonyms(sci),
            ]
            resolved = False
            for syn in synonyms:
                xc_name = _xc_lookup_direct(syn, api_key)
                if xc_name:
                    existing[sci] = {
                        "xc_name": xc_name,
                        "matched_name": syn,
                        "aliases": [syn, xc_name],
                    }
                    phase4_count += 1
                    resolved = True
                    break
            if not resolved:
                # Phase 5 inline: try English name as last resort
                en_name = rec.get("preferred_common_name", "")
                xc_name = _xc_lookup_english(en_name, api_key)
                if xc_name:
                    existing[sci] = {"xc_name": xc_name}
                    phase4_count += 1
                else:
                    existing[sci] = {"xc_name": None}
        progress.close()
        print(f"  Resolved {phase4_count} via GBIF/English fallback")
        save_json(existing, OUTPUT_FILE)

    _print_stats(existing, species)


def _print_stats(existing: dict, target: dict):
    """Print summary statistics."""
    total = len(existing)
    resolved = sum(1 for v in existing.values()
                   if v.get("xc_name") is not None)
    unresolved = total - resolved
    # Count name mismatches (where XC uses a different name)
    mismatches = sum(
        1 for s, v in existing.items()
        if v.get("xc_name") and v["xc_name"] != s
    )
    target_resolved = sum(1 for s in target
                          if existing.get(s, {}).get("xc_name") is not None)
    print(f"\nDone! {total} species in {OUTPUT_FILE.name}")
    print(f"  Resolved:       {resolved}")
    print(f"  Unresolved:     {unresolved}")
    print(f"  Name mismatches: {mismatches} (XC uses a different name)")
    if target:
        pct = 100 * target_resolved / len(target)
        print(f"  Target coverage: {target_resolved}/{len(target)} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
