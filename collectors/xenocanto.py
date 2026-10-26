#!/usr/bin/env python3
"""
Map species to their Xeno-Canto scientific names.

XC uses IOC taxonomy which may differ from our iNat/eBird-based names.
This collector finds the XC-equivalent name for each species.

Resolution cascade:
  1. Wikidata P2426 (XC species ID) via bulk SPARQL — covers ~31k species
  2. XC API v3 direct lookup by genus + epithet
  3. XC API epithet-only search within the species' XC groups (catches genus
     transfers; skipped for groups XC can't filter, e.g. reptiles)
  4. GBIF synonym lookup → retry XC API with alternate names
  5. XC API English name search within the species' XC groups (final fallback)

Fuzzy matches (3, 5) that equal another species' name or alias are rejected;
--revalidate drops such names cached by earlier runs and re-resolves them.

Input:  raw_data/taxonomy.json (or inat_data.json)
Output: raw_data/xc_data.json

Usage:
    python -m collectors.xenocanto [--limit N] [--group NAME] [--dry-run] [--new-only]
    python -m collectors.xenocanto --revalidate --retry-unresolved
"""

import argparse
import json
import time
import urllib.parse
import urllib.request

from tqdm import tqdm

from config import load_config, load_env_value
from collectors._common import (
    RAW_DIR, USER_AGENT,
    is_full_species_name, load_canonical_species, load_json, save_json,
    setup_shutdown, is_shutting_down,
    RateLimiter, TRUSTED_XC_METHODS, related_species_names, xc_record_method,
)

INAT_FILE = RAW_DIR / "inat_data.json"
OUTPUT_FILE = RAW_DIR / "xc_data.json"

XC_API_URL = "https://xeno-canto.org/api/3/recordings"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
GBIF_MATCH_URL = "https://api.gbif.org/v1/species/match"
GBIF_SYNONYMS_URL = "https://api.gbif.org/v1/species/{key}/synonyms"

_SPARQL_BATCH = 200

# Map taxon groups to the XC group tags their recordings are filed under.
# Fuzzy lookups (epithet, English name) are only accepted within these
# groups; a group without tags (reptiles) gets exact-name lookups only.
_INAT_TO_XC_GROUP = {
    "Aves": ("birds",),
    "Mammalia": ("bats", "land mammals"),
    "Insecta": ("grasshoppers",),
    "Amphibia": ("frogs",),
    "Reptilia": (),  # XC has no reptile group tag
}

# Groups where an epithet-only search is meaningful. XC files all insects
# under Orthoptera-centric groups, so a bee or beetle epithet would land on an
# unrelated grasshopper; reptiles have no XC group at all.
EPITHET_SEARCH_GROUPS = {"Aves", "Mammalia", "Amphibia"}

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

# Failed requests (as opposed to "no recordings") must never be cached as
# "species not on XC"; an outage would otherwise mark thousands unresolved.
_xc_failures = 0          # total failed requests this run
_xc_consecutive_fail = 0  # consecutive failed requests
_XC_MAX_CONSECUTIVE_FAIL = 20


def _xc_unavailable() -> bool:
    return _xc_consecutive_fail >= _XC_MAX_CONSECUTIVE_FAIL


def _xc_search(query: str, api_key: str) -> dict | None:
    """Query XC API v3. Returns response dict or None on error."""
    global _xc_failures, _xc_consecutive_fail
    if _xc_unavailable():
        _xc_failures += 1
        return None
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
    for attempt in range(2):
        _rate.acquire()
        try:
            # Broad epithet queries can take close to a minute on XC
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            _xc_consecutive_fail = 0
            return data
        except Exception:
            if attempt == 0:
                time.sleep(2)
    _xc_failures += 1
    _xc_consecutive_fail += 1
    if _xc_unavailable():
        print(f"\n  XC API unavailable ({_XC_MAX_CONSECUTIVE_FAIL} failed requests in a row) — "
              "stopping lookups; rerun later to continue.")
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
    """Search XC by gen:X sp:Y."""
    parts = scientific_name.split()
    if len(parts) != 2:
        return None
    gen, sp = parts
    data = _xc_search(f"gen:{gen} sp:{sp}", api_key)  # XC v3 separates tags with spaces
    if data:
        return _xc_name_from_response(data)
    return None


def _xc_lookup_epithet(scientific_name: str, xc_groups: tuple[str, ...],
                       api_key: str) -> str | None:
    """Search XC by epithet within the species' groups (catches genus transfers).

    Only accepted when exactly one XC species in the group carries the
    epithet; otherwise the first recording's genus would be arbitrary
    (e.g. five bird species are called "atricapillus").
    """
    parts = scientific_name.split()
    if len(parts) != 2 or not xc_groups:
        return None
    epithet = parts[1]
    found: set[str] = set()
    for xc_group in xc_groups:
        data = _xc_search(f'sp:{epithet} grp:"{xc_group}"', api_key)
        if not data or int(data.get("numRecordings") or 0) == 0:
            continue
        if str(data.get("numSpecies")) != "1":
            return None
        for r in data.get("recordings", []):
            gen = (r.get("gen") or "").strip()
            if (r.get("sp") or "").strip() == epithet and gen:
                found.add(f"{gen} {epithet}")
    return found.pop() if len(found) == 1 else None


def _xc_lookup_english(common_name: str, xc_groups: tuple[str, ...],
                       api_key: str) -> str | None:
    """Search XC by English name (exact match) within the species' groups."""
    if not common_name or not xc_groups:
        return None
    data = _xc_search(f'en:"={common_name}"', api_key)
    if not data:
        return None
    for r in data.get("recordings", []):
        gen = (r.get("gen") or "").strip()
        sp = (r.get("sp") or "").strip()
        if gen and sp and r.get("grp") in xc_groups:
            return f"{gen} {sp}"
    return None


def _name_owners(species: dict[str, dict]) -> dict[str, set[str]]:
    """Map every canonical name and alias to the species carrying it."""
    owners: dict[str, set[str]] = {}
    for sci, rec in species.items():
        for name in [sci, *rec.get("scientific_name_aliases", [])]:
            owners.setdefault(name, set()).add(sci)
    return owners


def _is_foreign_name(sci: str, xc_name: str, rec: dict,
                     owners: dict[str, set[str]], method: str = "") -> bool:
    """True if an XC name can't be this species: it is another species' name,
    a fuzzy match in a group XC can't filter by, or an unrelated name (new
    genus and epithet) without a trustworthy match method."""
    if xc_name == sci or xc_name in rec.get("scientific_name_aliases", []):
        return False
    if sci not in owners.get(xc_name, {sci}):
        return True
    if not _INAT_TO_XC_GROUP.get(rec.get("taxon_group", ""), ()):
        return True
    if method == "epithet":
        return True  # epithet match from before uniqueness was checked
    if method == "epithet_unique" and rec.get("taxon_group") not in EPITHET_SEARCH_GROUPS:
        return True
    return not related_species_names(sci, xc_name) and method not in TRUSTED_XC_METHODS


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
    parser.add_argument("--revalidate", action="store_true",
                        help="Drop cached XC names that belong to another species "
                             "(or fuzzy matches in groups XC can't filter) and re-resolve them")
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

    owners = _name_owners(species)

    if args.revalidate:
        foreign = [
            sci for sci in species
            if (existing.get(sci) or {}).get("xc_name")
            and _is_foreign_name(sci, existing[sci]["xc_name"], species[sci], owners,
                                 xc_record_method(existing[sci]))
        ]
        for sci in foreign:
            existing.pop(sci, None)
        print(f"  Revalidating: dropped {len(foreign)} XC names that match another species")

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
            if sci not in existing and not _is_foreign_name(sci, xc_name, species[sci], owners, "wikidata"):
                existing[sci] = {"xc_name": xc_name, "method": "wikidata"}
                phase1_count += 1
        print(f"  Resolved {phase1_count} via Wikidata")
        if phase1_count:
            save_json(existing, OUTPUT_FILE)

    if is_shutting_down() or _xc_unavailable():
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
            if is_shutting_down() or _xc_unavailable():
                break
            xc_name = _xc_lookup_direct(sci, api_key)
            if xc_name:
                existing[sci] = {"xc_name": xc_name, "method": "direct"}
                phase2_count += 1
            if phase2_count % 200 == 0 and phase2_count:
                save_json(existing, OUTPUT_FILE)
        progress.close()
        print(f"  Resolved {phase2_count} via XC direct")
        save_json(existing, OUTPUT_FILE)

    if is_shutting_down() or _xc_unavailable():
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
            if is_shutting_down() or _xc_unavailable():
                break
            rec = species[sci]
            if rec.get("taxon_group") not in EPITHET_SEARCH_GROUPS:
                continue
            xc_groups = _INAT_TO_XC_GROUP.get(rec.get("taxon_group", ""), ())
            xc_name = _xc_lookup_epithet(sci, xc_groups, api_key)
            if xc_name and not _is_foreign_name(sci, xc_name, rec, owners, "epithet_unique"):
                existing[sci] = {"xc_name": xc_name, "method": "epithet_unique"}
                phase3_count += 1
        progress.close()
        print(f"  Resolved {phase3_count} via epithet search")
        if phase3_count:
            save_json(existing, OUTPUT_FILE)

    if is_shutting_down() or _xc_unavailable():
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
            if is_shutting_down() or _xc_unavailable():
                break
            rec = species[sci]
            failures_before = _xc_failures
            synonyms = [
                *rec.get("scientific_name_aliases", []),
                *_gbif_synonyms(sci),
            ]
            resolved = False
            for syn in synonyms:
                if sci not in owners.get(syn, {sci}):
                    continue  # another species' name, not a synonym of this one
                xc_name = _xc_lookup_direct(syn, api_key)
                if xc_name:
                    existing[sci] = {
                        "xc_name": xc_name,
                        "method": "synonym",
                        "matched_name": syn,
                        "aliases": [syn, xc_name],
                    }
                    phase4_count += 1
                    resolved = True
                    break
            if not resolved:
                # Phase 5 inline: try English name as last resort
                en_name = rec.get("preferred_common_name", "")
                xc_groups = _INAT_TO_XC_GROUP.get(rec.get("taxon_group", ""), ())
                xc_name = _xc_lookup_english(en_name, xc_groups, api_key)
                if xc_name and not _is_foreign_name(sci, xc_name, rec, owners, "english"):
                    existing[sci] = {"xc_name": xc_name, "method": "english"}
                    phase4_count += 1
                elif _xc_failures == failures_before:
                    # Only a clean "no match" is cached; failed lookups are retried next run
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
