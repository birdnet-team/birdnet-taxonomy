#!/usr/bin/env python3
"""
Map species to their observation.org species IDs.

observation.org (formerly Waarneming.nl) uses AviList taxonomy for birds.
This collector finds the observation.org species ID for each species so
users can link directly to species pages.

Resolution cascade:
  1. Direct API search by scientific name
  2. GBIF synonym lookup → retry with alternate names

Input:  raw_data/inat_data.json
Output: raw_data/observationorg_data.json

Usage:
    python -m collectors.observationorg [--limit N] [--group NAME] [--dry-run] [--new-only]
"""

import argparse
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT,
    is_full_species_name, load_json, save_json,
    setup_shutdown, is_shutting_down,
    RateLimiter,
)

INAT_FILE = RAW_DIR / "inat_data.json"
OUTPUT_FILE = RAW_DIR / "observationorg_data.json"

OBS_API_URL = "https://observation.org/api/v1/species/search/"
GBIF_MATCH_URL = "https://api.gbif.org/v1/species/match"
GBIF_SYNONYMS_URL = "https://api.gbif.org/v1/species/{key}/synonyms"

# observation.org blocks generic bot user agents; use a browser-like one
_OBS_USER_AGENT = ("Mozilla/5.0 (compatible; BirdNET/1.0; "
                   "+https://github.com/birdnet-team/species-data)")

_rate = RateLimiter(10)   # no documented rate limit
_gbif_rate = RateLimiter(20)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _obs_search(scientific_name: str) -> dict | None:
    """Search observation.org species API. Returns first matching result."""
    _rate.acquire()
    params = urllib.parse.urlencode({"q": scientific_name, "format": "json"})
    url = f"{OBS_API_URL}?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _OBS_USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None

    # Find exact species-level match
    for r in results:
        if r.get("type") != "S":
            continue
        r_sci = (r.get("scientific_name") or "").strip()
        if r_sci.lower() == scientific_name.lower():
            return r
    return None


def _obs_lookup(scientific_name: str) -> int | None:
    """Look up a species and return its observation.org ID, or None."""
    result = _obs_search(scientific_name)
    if result:
        return result.get("id")
    return None


# ---------------------------------------------------------------------------
# GBIF synonym resolution
# ---------------------------------------------------------------------------

def _gbif_get_synonyms(scientific_name: str) -> list[str]:
    """Get alternate names from GBIF for a species."""
    _gbif_rate.acquire()
    params = urllib.parse.urlencode({
        "name": scientific_name,
        "strict": "true",
        "kingdom": "Animalia",
    })
    url = f"{GBIF_MATCH_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []

    usage_key = data.get("usageKey")
    if not usage_key:
        return []

    # Also collect the accepted name if different
    names = []
    accepted = (data.get("species") or "").strip()
    if accepted and accepted.lower() != scientific_name.lower():
        names.append(accepted)

    # Fetch synonyms list
    _gbif_rate.acquire()
    syn_url = GBIF_SYNONYMS_URL.format(key=usage_key) + "?limit=20"
    req = urllib.request.Request(syn_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            syn_data = json.loads(resp.read())
    except Exception:
        return names

    for rec in syn_data.get("results", []):
        syn = (rec.get("species") or rec.get("canonicalName") or "").strip()
        if syn and is_full_species_name(syn) and syn.lower() != scientific_name.lower():
            if syn not in names:
                names.append(syn)

    return names


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _print_stats(existing: dict) -> None:
    total = len(existing)
    resolved = sum(1 for v in existing.values()
                   if v.get("observationorg_id") is not None)
    unresolved = total - resolved
    print(f"\n  Total:      {total}")
    print(f"  Resolved:   {resolved}")
    print(f"  Unresolved: {unresolved}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map species to observation.org IDs")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--group", type=str, default="",
                        help="Only process this taxon group (e.g. Aves)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel workers (default: 8)")
    parser.add_argument("--save-every", type=int, default=200,
                        help="Save every N completed species (default: 200)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without saving")
    parser.add_argument("--new-only", action="store_true",
                        help="Skip species already in output file")
    args = parser.parse_args()

    setup_shutdown()
    cfg = load_config()

    # Load input data
    inat = load_json(INAT_FILE)
    if not inat:
        print("ERROR: No iNat data. Run: python -m collectors.inat")
        raise SystemExit(1)

    existing: dict = load_json(OUTPUT_FILE) or {}
    print(f"observation.org collector")
    print(f"  Input species: {len(inat)}")
    print(f"  Existing:      {len(existing)}")

    # Build work list
    species_list: list[tuple[str, dict]] = []
    for sci, rec in inat.items():
        if not is_full_species_name(sci):
            continue
        if args.group and rec.get("taxon_group") != args.group:
            continue
        if args.new_only and sci in existing:
            continue
        species_list.append((sci, rec))

    if args.limit:
        species_list = species_list[:args.limit]

    to_process = [(s, r) for s, r in species_list if s not in existing]
    print(f"  To process:    {len(to_process)}")

    if args.dry_run:
        for sci, _ in to_process[:20]:
            print(f"    {sci}")
        if len(to_process) > 20:
            print(f"    ... and {len(to_process) - 20} more")
        return

    # ------------------------------------------------------------------
    # Phase 1: Direct API search by scientific name
    # ------------------------------------------------------------------
    phase1_hits = 0
    remaining = [(s, r) for s, r in to_process if s not in existing]
    if remaining:
        print(f"\n  Phase 1: Direct API search ({len(remaining)} species)...")
        unsaved = 0
        pbar = tqdm(total=len(remaining), desc="  observation.org",
                    disable=None)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for sci, rec in remaining:
                if is_shutting_down():
                    break
                futures[pool.submit(_obs_lookup, sci)] = sci

            for future in as_completed(futures):
                sci = futures[future]
                try:
                    obs_id = future.result()
                    if obs_id is not None:
                        existing[sci] = {"observationorg_id": obs_id}
                        phase1_hits += 1
                except Exception as exc:
                    tqdm.write(f"  ERROR {sci}: {exc}")
                pbar.update(1)
                unsaved += 1
                if unsaved >= args.save_every:
                    save_json(existing, OUTPUT_FILE)
                    unsaved = 0
        pbar.close()
        save_json(existing, OUTPUT_FILE)
        print(f"    Matched: {phase1_hits}")

    if is_shutting_down():
        _print_stats(existing)
        return

    # ------------------------------------------------------------------
    # Phase 2: GBIF synonym fallback
    # ------------------------------------------------------------------
    unresolved = [(s, r) for s, r in to_process
                  if s not in existing]
    phase2_hits = 0

    def _resolve_via_gbif(sci: str) -> tuple[str, int | None]:
        """Try GBIF synonyms → observation.org lookup. Returns (sci, id)."""
        synonyms = _gbif_get_synonyms(sci)
        for alt in synonyms:
            obs_id = _obs_lookup(alt)
            if obs_id is not None:
                return sci, obs_id
        return sci, None

    if unresolved:
        print(f"\n  Phase 2: GBIF synonym fallback ({len(unresolved)} "
              f"species)...")
        unsaved = 0
        pbar = tqdm(total=len(unresolved), desc="  GBIF→obs.org",
                    disable=None)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for sci, rec in unresolved:
                if is_shutting_down():
                    break
                futures[pool.submit(_resolve_via_gbif, sci)] = sci

            for future in as_completed(futures):
                sci = futures[future]
                try:
                    _, obs_id = future.result()
                    if obs_id is not None:
                        existing[sci] = {"observationorg_id": obs_id}
                        phase2_hits += 1
                    else:
                        existing[sci] = {"observationorg_id": None}
                except Exception as exc:
                    tqdm.write(f"  ERROR {sci}: {exc}")
                    existing[sci] = {"observationorg_id": None}
                pbar.update(1)
                unsaved += 1
                if unsaved >= args.save_every:
                    save_json(existing, OUTPUT_FILE)
                    unsaved = 0
        pbar.close()
        save_json(existing, OUTPUT_FILE)
        print(f"    Matched: {phase2_hits}")

    _print_stats(existing)
    print()


if __name__ == "__main__":
    main()
