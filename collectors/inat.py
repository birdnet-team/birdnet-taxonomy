#!/usr/bin/env python3
"""
Fetch iNaturalist taxonomy for configured taxon groups.

Two modes per group (controlled by sounds_only in config):

  sounds_only: true  — Bulk query observations/species_counts?sounds=true to
      get species with sound recordings, filtered by min_observations.
  sounds_only: false — Paginate the full taxa API to get ALL species
      (e.g. Aves), then enrich with sound observation counts.

In both modes, full taxon details (common names, photos, etc.) are fetched
via the taxa API in batches of 30 IDs.

After group fetching, an AviList reconciliation phase looks up any species
present in the AviList checklist but missing from iNat data, and adds them.

An observation photo fallback phase queries the iNat observations API for
CC-licensed photos for species whose default taxon photo is missing or
not permissively licensed.

Output: raw_data/inat_data.json (incremental, resumable)

Usage:
    python -m collectors.inat [--group NAME] [--limit N] [--dry-run]
"""

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from config import load_config
from collectors._common import (
    ROOT, RAW_DIR, USER_AGENT, ACCEPTABLE_LICENSES,
    setup_shutdown, is_shutting_down,
    is_full_species_name, load_json, save_json,
)

OUTPUT_FILE = RAW_DIR / "inat_data.json"
PRIORITY_SPECIES_FILE = ROOT / "overrides" / "priority_species.csv"
CACHE_DIR = RAW_DIR / "cache"
SPECIES_COUNTS_URL = "https://api.inaturalist.org/v1/observations/species_counts"
TAXA_URL = "https://api.inaturalist.org/v1/taxa"
COUNTS_PER_PAGE = 500  # max for species_counts endpoint
TAXA_BATCH_SIZE = 30   # IDs per taxa batch request

_shutdown = setup_shutdown()


def _api_get(url: str, timeout: int = 60) -> dict | None:
    """Make a GET request to the iNat API. Returns parsed JSON or None."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as e:
        print(f"  ERROR: {e} — {url[:120]}")
        return None


def photo_url_large(url: str) -> str:
    """Convert iNat photo URL from square/medium to large."""
    if not url:
        return ""
    return url.replace("/square.", "/large.").replace("/medium.", "/large.")


# ── Cache helpers ──────────────────────────────────────────────────────

def _cache_path(kind: str, taxon_id: int) -> Path:
    """Return path for a cache file, e.g. raw_data/cache/sound_counts_3.json."""
    return CACHE_DIR / f"{kind}_{taxon_id}.json"


def _cache_age_str(timestamp: str) -> str:
    """Human-readable age from an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{hours:.1f}h ago"
        return f"{delta.days}d ago"
    except Exception:
        return "unknown age"


def _load_cache(kind: str, taxon_id: int) -> tuple[list | None, str]:
    """Load cached Phase 1 data. Returns (data_list, timestamp) or (None, "")."""
    path = _cache_path(kind, taxon_id)
    if not path.exists():
        return None, ""
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        return cached.get("data"), cached.get("timestamp", "")
    except Exception:
        return None, ""


def _save_cache(kind: str, taxon_id: int, data: list, **meta):
    """Save Phase 1 data to cache with timestamp and optional metadata."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "taxon_id": taxon_id,
        **meta,
        "data": data,
    }
    save_json(payload, _cache_path(kind, taxon_id))


# ── Phase 1: sound observation counts ──────────────────────────────────

def fetch_sound_species(taxon_id: int, min_obs: int,
                        delay: float) -> list[dict]:
    """Fetch all species with sound observations for a taxon group.

    Returns list of {id, name, sound_count, observations_count, ...}
    sorted by sound_count descending. Stops when sound_count < min_obs.
    """
    species = []
    page = 1

    while not is_shutting_down():
        url = (
            f"{SPECIES_COUNTS_URL}"
            f"?taxon_id={taxon_id}&sounds=true"
            f"&per_page={COUNTS_PER_PAGE}&page={page}"
        )
        data = _api_get(url)
        if not data:
            # Retry once
            time.sleep(delay * 3)
            data = _api_get(url)
            if not data:
                print(f"  FAILED page {page}, stopping")
                break

        results = data.get("results", [])
        if not results:
            break

        total = data.get("total_results", 0)
        total_pages = (total + COUNTS_PER_PAGE - 1) // COUNTS_PER_PAGE
        hit_cutoff = False

        for r in results:
            sound_count = r.get("count", 0)
            if min_obs and sound_count < min_obs:
                hit_cutoff = True
                break

            taxon = r.get("taxon", {})
            sci_name = taxon.get("name", "")
            if not is_full_species_name(sci_name):
                continue
            species.append({
                "id": taxon.get("id"),
                "name": sci_name,
                "sound_count": sound_count,
                "observations_count": taxon.get("observations_count", 0),
            })

        print(
            f"  Sound counts page {page}/{total_pages} — "
            f"{len(results)} results, {len(species)} species so far",
            flush=True,
        )

        if hit_cutoff:
            print(f"  Reached min_observations cutoff ({min_obs} sound obs)")
            break
        if page >= total_pages:
            break

        page += 1
        time.sleep(delay)

    return species


def fetch_observed_species(taxon_id: int, min_obs: int,
                           delay: float) -> list[dict]:
    """Fetch species with at least min_obs total iNat observations."""
    species = []
    page = 1

    while not is_shutting_down():
        url = (
            f"{SPECIES_COUNTS_URL}"
            f"?taxon_id={taxon_id}"
            f"&per_page={COUNTS_PER_PAGE}&page={page}"
        )
        data = _api_get(url)
        if not data:
            time.sleep(delay * 3)
            data = _api_get(url)
            if not data:
                print(f"  FAILED observed page {page}, stopping")
                break

        results = data.get("results", [])
        if not results:
            break

        total = data.get("total_results", 0)
        total_pages = (total + COUNTS_PER_PAGE - 1) // COUNTS_PER_PAGE
        hit_cutoff = False

        for r in results:
            obs_count = r.get("count", 0)
            if min_obs and obs_count < min_obs:
                hit_cutoff = True
                break

            taxon = r.get("taxon", {})
            sci_name = taxon.get("name", "")
            if not is_full_species_name(sci_name):
                continue
            species.append({
                "id": taxon.get("id"),
                "name": sci_name,
                "sound_count": 0,
                "observations_count": obs_count,
            })

        print(
            f"  Observation counts page {page}/{total_pages} — "
            f"{len(results)} results, {len(species)} species so far",
            flush=True,
        )

        if hit_cutoff:
            print(f"  Reached min_total_observations cutoff ({min_obs} observations)")
            break
        if page >= total_pages:
            break

        page += 1
        time.sleep(delay)

    return species


# ── Phase 2: full taxon details ────────────────────────────────────────

def fetch_taxa_batch(taxon_ids: list[int], all_names: bool,
                     delay: float) -> list[dict]:
    """Fetch full taxon records for a batch of IDs."""
    ids_str = ",".join(str(i) for i in taxon_ids)
    url = f"{TAXA_URL}/{ids_str}?all_names={'true' if all_names else 'false'}"
    data = _api_get(url)
    if not data:
        time.sleep(delay * 3)
        data = _api_get(url)
    if not data:
        return []
    return data.get("results", [])


def extract_record(result: dict, group_name: str, sound_count: int) -> dict:
    """Extract relevant fields from an iNat taxa API result."""
    # Common names by locale
    common_names = {}
    for name_entry in result.get("names", []):
        if not name_entry.get("is_valid", False):
            continue
        locale = name_entry.get("locale", "")
        lexicon = name_entry.get("lexicon", "")
        if locale == "sci" or lexicon in (
            "scientific-names", "aou-4-letter-codes", "aou-6-letter-codes"
        ):
            continue
        if locale and locale != "und":
            if locale not in common_names:
                common_names[locale] = name_entry.get("name", "")

    # Photo URL — use large version
    photo = result.get("default_photo") or {}
    photo_url = (
        photo.get("url", "")
        or photo.get("medium_url", "")
        or photo.get("square_url", "")
    )
    photo_url = photo_url_large(photo_url)

    return {
        "inat_id": result.get("id"),
        "taxon_group": group_name,
        "iconic_taxon_name": result.get("iconic_taxon_name", ""),
        "common_names": common_names,
        "wikipedia_url": result.get("wikipedia_url", ""),
        "image_url": photo_url,
        "image_attribution": photo.get("attribution", ""),
        "image_license": photo.get("license_code", ""),
        "observations_count": result.get("observations_count", 0),
        "sound_observations_count": sound_count,
        "preferred_common_name": result.get("preferred_common_name", ""),
        "extinct": bool(result.get("extinct", False)),
    }


# ── Group orchestration ───────────────────────────────────────────────

def fetch_all_species(taxon_id: int, per_page: int, all_names: bool,
                      delay: float) -> list[dict]:
    """Fetch ALL species for a taxon group via the taxa API.

    Paginates through the full taxa listing sorted by observation count.
    Returns list of full taxon result dicts.
    """
    base = f"{TAXA_URL}?is_active=true&rank=species"
    all_results = []
    page = 1

    while not is_shutting_down():
        url = (
            f"{base}"
            f"&all_names={'true' if all_names else 'false'}"
            f"&order=desc&order_by=observations_count"
            f"&taxon_id={taxon_id}"
            f"&per_page={per_page}&page={page}"
        )
        data = _api_get(url)
        if not data:
            time.sleep(delay * 3)
            data = _api_get(url)
            if not data:
                print(f"  FAILED page {page}, stopping")
                break

        results = data.get("results", [])
        if not results:
            break

        total = data.get("total_results", 0)
        total_pages = (total + per_page - 1) // per_page
        all_results.extend(results)

        print(
            f"  Taxa page {page}/{total_pages} — "
            f"{len(results)} species, {len(all_results)} total",
            flush=True,
        )

        if page >= total_pages:
            break

        page += 1
        time.sleep(delay)

    return all_results


def fetch_group(group: dict, existing: dict, cfg: dict,
                limit: int = 0, save_every: int = 500,
                refresh: bool = False, new_only: bool = False) -> int:
    """Fetch species for one taxon group. Returns count of new records.

    If sounds_only is true: bulk-query species_counts for species with sounds,
    then batch-fetch full taxon details.
    If sounds_only is false: paginate all species via taxa API, then enrich
    with sound counts.

    Set refresh=True to bypass cached Phase 1 data.
    """
    group_name = group["name"]
    total_new = _fetch_taxon_scope(
        group_name=group_name,
        scope=group,
        cfg=cfg,
        existing=existing,
        limit=limit,
        save_every=save_every,
        refresh=refresh,
        new_only=new_only,
    )

    for sub in group.get("subtaxa", []) or []:
        if is_shutting_down():
            break
        total_new += _fetch_taxon_scope(
            group_name=group_name,
            scope=sub,
            cfg=cfg,
            existing=existing,
            limit=limit,
            save_every=save_every,
            refresh=refresh,
            new_only=new_only,
        )

    return total_new


def _fetch_taxon_scope(group_name: str, scope: dict, cfg: dict,
                       existing: dict, limit: int, save_every: int,
                       refresh: bool, new_only: bool) -> int:
    taxon_id = scope["taxon_id"]
    sounds_only = scope.get("sounds_only", True)
    min_obs = scope.get("min_observations", 0)
    min_total_obs = scope.get("min_total_observations", 0)
    inat_cfg = cfg.get("inat", {})
    all_names = inat_cfg.get("all_names", True)
    delay = inat_cfg.get("request_delay", 1.1)

    print(f"\n{'='*60}")
    if scope.get("name") and scope.get("name") != group_name:
        print(f"Scope: {group_name} / {scope['name']} ({scope.get('rank', 'taxon')})")

    if sounds_only:
        return _fetch_sounds_only(group_name, taxon_id, min_obs,
                                  all_names, delay, existing, limit,
                                  save_every, refresh, new_only,
                                  min_total_obs=min_total_obs)
    else:
        return _fetch_all_species(group_name, taxon_id,
                                  all_names, delay, existing, limit,
                                  save_every, refresh, new_only)


def _fetch_sounds_only(group_name: str, taxon_id: int, min_obs: int,
                       all_names: bool, delay: float, existing: dict,
                       limit: int, save_every: int,
                       refresh: bool = False,
                       new_only: bool = False,
                       min_total_obs: int = 0) -> int:
    """Fetch only species with sound observations (sounds_only=true groups)."""
    min_obs_label = f"≥{min_obs} sound obs" if min_obs else "all with sounds"
    print(f"Fetching {group_name} (taxon_id={taxon_id}, {min_obs_label})")

    # Phase 1: get species with sound observations (cached)
    sound_species = None
    if not refresh:
        cached, ts = _load_cache("sound_counts", taxon_id)
        if cached is not None:
            sound_species = cached
            # Apply min_observations filter to cached data
            if min_obs:
                sound_species = [s for s in sound_species
                                 if s.get("sound_count", 0) >= min_obs]
            print(f"  Phase 1: Using cached sound counts "
                  f"({_cache_age_str(ts)}, {len(sound_species)} species)")

    if sound_species is None:
        print("  Phase 1: Fetching sound observation counts...")
        sound_species = fetch_sound_species(taxon_id, min_obs=0, delay=delay)
        # Cache ALL sound species (unfiltered) so cache works with any min_obs
        _save_cache("sound_counts", taxon_id, sound_species,
                    total_unfiltered=len(sound_species))
        # Now apply min_observations filter
        if min_obs:
            sound_species = [s for s in sound_species
                             if s.get("sound_count", 0) >= min_obs]
    print(f"  Found {len(sound_species)} species with sound observations")

    observed_species = []
    if min_total_obs:
        if not refresh:
            cached, ts = _load_cache("observation_counts", taxon_id)
            if cached is not None:
                observed_species = [
                    s for s in cached
                    if s.get("observations_count", 0) >= min_total_obs
                ]
                print(f"  Phase 1b: Using cached observation counts "
                      f"({_cache_age_str(ts)}, {len(observed_species)} species)")
        if not observed_species:
            print("  Phase 1b: Fetching total observation counts...")
            observed_all = fetch_observed_species(taxon_id, min_obs=0, delay=delay)
            _save_cache("observation_counts", taxon_id, observed_all,
                        total_unfiltered=len(observed_all))
            observed_species = [
                s for s in observed_all
                if s.get("observations_count", 0) >= min_total_obs
            ]
        print(f"  Found {len(observed_species)} species with ≥{min_total_obs} observations")

    if observed_species:
        merged: dict[int, dict] = {}
        for rec in observed_species:
            if rec.get("id"):
                merged[rec["id"]] = rec
        for rec in sound_species:
            tid = rec.get("id")
            if not tid:
                continue
            existing_rec = merged.get(tid, {})
            merged[tid] = {
                **existing_rec,
                **rec,
                "observations_count": max(
                    rec.get("observations_count", 0),
                    existing_rec.get("observations_count", 0),
                ),
                "sound_count": rec.get("sound_count", existing_rec.get("sound_count", 0)),
            }
        sound_species = list(merged.values())
        print(f"  Combined sound/observation scope: {len(sound_species)} species")

    if is_shutting_down():
        return 0

    sound_lookup = {s["id"]: s["sound_count"] for s in sound_species}
    to_fetch = [
        s for s in sound_species
        if s["name"] not in existing
        or existing[s["name"]].get("inat_id") is None
    ]
    if limit:
        to_fetch = to_fetch[:limit]

    existing_in_group = sum(
        1 for v in existing.values()
        if v.get("taxon_group") == group_name and v.get("inat_id") is not None
    )
    print(f"  Already have: {existing_in_group} from previous runs")
    print(f"  Need to fetch details for: {len(to_fetch)} new species")

    if not to_fetch:
        if new_only:
            return 0
        updated = _update_sound_counts(existing, sound_lookup, group_name)
        if updated:
            save_json(existing, OUTPUT_FILE)
            print(f"  Updated sound counts for {updated} existing species")
        return 0

    # Phase 2: batch-fetch full taxon details
    print("  Phase 2: Fetching full taxon details...")
    new_count = _batch_fetch_taxa(to_fetch, group_name, all_names, delay,
                                  existing, save_every)

    updated = 0 if new_only else _update_sound_counts(existing, sound_lookup, group_name)
    save_json(existing, OUTPUT_FILE)
    print(f"  Done {group_name}: {new_count} new species added")
    if updated:
        print(f"  Updated sound counts for {updated} existing species")
    return new_count


def _fetch_all_species(group_name: str, taxon_id: int,
                       all_names: bool, delay: float, existing: dict,
                       limit: int, save_every: int,
                       refresh: bool = False,
                       new_only: bool = False) -> int:
    """Fetch ALL species for a group (sounds_only=false), enrich with sound counts."""
    print(f"Fetching {group_name} (taxon_id={taxon_id}, all species)")

    existing_in_group = sum(
        1 for v in existing.values()
        if v.get("taxon_group") == group_name and v.get("inat_id") is not None
    )
    print(f"  Already have: {existing_in_group} from previous runs")

    # Phase 1: paginate all species from taxa API (cached)
    all_results = None
    if not refresh:
        cached, ts = _load_cache("taxa", taxon_id)
        if cached is not None:
            all_results = cached
            print(f"  Phase 1: Using cached taxa list "
                  f"({_cache_age_str(ts)}, {len(all_results)} species)")

    if all_results is None:
        print("  Phase 1: Fetching all species from taxa API...")
        all_results = fetch_all_species(taxon_id, per_page=200,
                                        all_names=all_names, delay=delay)
        _save_cache("taxa", taxon_id, all_results,
                    all_names=all_names)
    print(f"  Found {len(all_results)} total species")

    if is_shutting_down():
        return 0

    # Extract records for new species
    new_count = 0
    for result in all_results:
        sci_name = result.get("name", "")
        if not sci_name:
            continue
        if sci_name in existing and existing[sci_name].get("inat_id") is not None:
            continue
        record = extract_record(result, group_name, sound_count=0)
        existing[sci_name] = record
        new_count += 1
        if limit and new_count >= limit:
            break
        if new_count > 0 and new_count % save_every == 0:
            save_json(existing, OUTPUT_FILE)

    print(f"  {new_count} new species added")

    if is_shutting_down():
        save_json(existing, OUTPUT_FILE)
        return new_count

    # Phase 2: enrich with sound observation counts (cached)
    sound_species = None
    if not refresh:
        cached, ts = _load_cache("sound_counts", taxon_id)
        if cached is not None:
            sound_species = cached
            print(f"  Phase 2: Using cached sound counts "
                  f"({_cache_age_str(ts)}, {len(sound_species)} species)")

    if sound_species is None:
        print("  Phase 2: Fetching sound observation counts...")
        sound_species = fetch_sound_species(taxon_id, min_obs=0, delay=delay)
        _save_cache("sound_counts", taxon_id, sound_species,
                    total_unfiltered=len(sound_species))

    sound_lookup = {s["id"]: s["sound_count"] for s in sound_species}
    updated = 0 if new_only else _update_sound_counts(existing, sound_lookup, group_name)

    save_json(existing, OUTPUT_FILE)
    print(f"  Done {group_name}: {new_count} new species, {updated} sound counts updated")
    return new_count


def _batch_fetch_taxa(to_fetch: list[dict], group_name: str, all_names: bool,
                      delay: float, existing: dict, save_every: int) -> int:
    """Batch-fetch full taxon details for a list of species."""
    new_count = 0
    batches = [
        to_fetch[i:i + TAXA_BATCH_SIZE]
        for i in range(0, len(to_fetch), TAXA_BATCH_SIZE)
    ]

    for batch_idx, batch in enumerate(batches):
        if is_shutting_down():
            break

        batch_ids = [s["id"] for s in batch]
        batch_sounds = {s["id"]: s["sound_count"] for s in batch}
        results = fetch_taxa_batch(batch_ids, all_names, delay)

        for result in results:
            sci_name = result.get("name", "")
            if not sci_name:
                continue
            tid = result.get("id")
            record = extract_record(result, group_name, batch_sounds.get(tid, 0))
            existing[sci_name] = record
            new_count += 1

        print(
            f"  Batch {batch_idx + 1}/{len(batches)} — "
            f"{len(results)} taxa, {new_count} total new",
            flush=True,
        )

        if new_count > 0 and new_count % save_every < TAXA_BATCH_SIZE:
            save_json(existing, OUTPUT_FILE)

        time.sleep(delay)

    return new_count


def _update_sound_counts(existing: dict, sound_lookup: dict[int, int],
                         group_name: str) -> int:
    """Update sound_observations_count for existing species from latest counts."""
    updated = 0
    for sci, rec in existing.items():
        if rec.get("taxon_group") != group_name:
            continue
        inat_id = rec.get("inat_id")
        if inat_id and inat_id in sound_lookup:
            new_count = sound_lookup[inat_id]
            if rec.get("sound_observations_count") != new_count:
                rec["sound_observations_count"] = new_count
                updated += 1
    return updated


# ── AviList reconciliation ─────────────────────────────────────────────

def _load_avilist_species(cfg: dict) -> set[str]:
    """Load species-rank scientific names from the AviList CSV."""
    avilist_cfg = cfg.get("avilist", {})
    csv_file = avilist_cfg.get("csv_file", "")
    if not csv_file:
        return set()
    csv_path = RAW_DIR / csv_file
    if not csv_path.exists():
        return set()

    species = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("Taxon_rank") == "species":
                name = row.get("Scientific_name", "").strip()
                if name:
                    species.add(name)
    return species


def _search_taxon(name: str, delay: float) -> dict | None:
    """Search iNat for an exact species name. Returns the taxon dict or None."""
    encoded = quote(name)
    url = f"{TAXA_URL}?q={encoded}&rank=species&per_page=5"
    data = _api_get(url)
    if not data:
        time.sleep(delay * 2)
        data = _api_get(url)
    if not data:
        return None

    for r in data.get("results", []):
        if r.get("name") == name and r.get("is_active"):
            return r

    # Fallback: try autocomplete endpoint
    url2 = f"{TAXA_URL}/autocomplete?q={encoded}&rank=species&per_page=5"
    data2 = _api_get(url2)
    if data2:
        for r in data2.get("results", []):
            if r.get("name") == name and r.get("is_active"):
                return r

    return None


def reconcile_avilist(existing: dict, cfg: dict, delay: float,
                     all_names: bool, limit: int = 0,
                     save_every: int = 200) -> int:
    """Look up AviList species not yet in iNat data and add them.

    Uses the iNat taxa search API to find each missing species by name.
    Returns the count of newly added species.
    """
    print(f"\n{'='*60}")
    print("AviList reconciliation")

    avi_species = _load_avilist_species(cfg)
    if not avi_species:
        print("  No AviList CSV found — run 'python -m collectors.avilist' first")
        return 0

    missing = sorted(name for name in avi_species if name not in existing)
    print(f"  AviList species: {len(avi_species)}")
    print(f"  Already in iNat data: {len(avi_species) - len(missing)}")
    print(f"  Missing: {len(missing)}")

    if not missing:
        print("  Nothing to reconcile!")
        return 0

    if limit:
        missing = missing[:limit]
        print(f"  Limited to first {limit}")

    # Look up each missing species via iNat taxa API
    # Use batch detail fetches where possible: collect IDs first,
    # then batch-fetch full records with all_names
    print(f"  Looking up {len(missing)} species on iNat...")

    found_taxa = []      # (sci_name, taxon_id)
    not_found = []       # sci_names not matched
    new_count = 0

    for i, name in enumerate(missing):
        if is_shutting_down():
            break

        result = _search_taxon(name, delay)
        if result:
            found_taxa.append((name, result.get("id")))
        else:
            not_found.append(name)

        if (i + 1) % 50 == 0 or i == len(missing) - 1:
            print(
                f"  Searched {i + 1}/{len(missing)} — "
                f"{len(found_taxa)} found, {len(not_found)} not found",
                flush=True,
            )

        time.sleep(delay)

    if is_shutting_down() and not found_taxa:
        return 0

    print(f"  Matched {len(found_taxa)} species, "
          f"{len(not_found)} not found on iNat")

    if not_found:
        show = not_found[:20]
        print(f"  Not found (first {len(show)}):")
        for name in show:
            print(f"    {name}")
        if len(not_found) > 20:
            print(f"    ... and {len(not_found) - 20} more")

    if not found_taxa:
        return 0

    # Batch-fetch full taxon details for all matched species
    print(f"  Fetching full details for {len(found_taxa)} species...")
    batches = [
        found_taxa[i:i + TAXA_BATCH_SIZE]
        for i in range(0, len(found_taxa), TAXA_BATCH_SIZE)
    ]

    for batch_idx, batch in enumerate(batches):
        if is_shutting_down():
            break

        batch_ids = [tid for _, tid in batch]
        batch_names = {tid: name for name, tid in batch}
        results = fetch_taxa_batch(batch_ids, all_names, delay)

        for result in results:
            sci_name = result.get("name", "")
            tid = result.get("id")
            # Use the AviList name if the iNat name matches one of our targets
            if tid in batch_names:
                sci_name = batch_names[tid]
            if (
                not sci_name
                or sci_name in existing
                or result.get("rank") != "species"
                or not is_full_species_name(sci_name)
            ):
                continue

            record = extract_record(result, "Aves", sound_count=0)
            existing[sci_name] = record
            new_count += 1

        if (batch_idx + 1) % 10 == 0 or batch_idx == len(batches) - 1:
            print(
                f"  Detail batch {batch_idx + 1}/{len(batches)} — "
                f"{new_count} new records",
                flush=True,
            )

        if new_count > 0 and new_count % save_every < TAXA_BATCH_SIZE:
            save_json(existing, OUTPUT_FILE)

        time.sleep(delay)

    save_json(existing, OUTPUT_FILE)
    print(f"  AviList reconciliation complete: {new_count} species added")
    return new_count


# ── Priority species ──────────────────────────────────────────────────

def load_priority_species() -> list[dict]:
    """Load reviewed forced-inclusion species from overrides."""
    if not PRIORITY_SPECIES_FILE.exists():
        return []

    required = {"scientific_name", "taxon_group", "source", "reason"}
    rows: list[dict] = []
    with open(PRIORITY_SPECIES_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing_cols = required - set(reader.fieldnames or [])
        if missing_cols:
            raise ValueError(
                f"{PRIORITY_SPECIES_FILE}: missing columns: {', '.join(sorted(missing_cols))}"
            )
        for line_no, row in enumerate(reader, start=2):
            sci = (row.get("scientific_name") or "").strip()
            group = (row.get("taxon_group") or "").strip()
            if not is_full_species_name(sci):
                raise ValueError(
                    f"{PRIORITY_SPECIES_FILE}: line {line_no}: invalid scientific_name '{sci}'"
                )
            if not group:
                raise ValueError(
                    f"{PRIORITY_SPECIES_FILE}: line {line_no}: taxon_group is required"
                )
            inat_raw = (row.get("inat_id") or "").strip()
            inat_id = None
            if inat_raw:
                try:
                    inat_id = int(inat_raw)
                except ValueError as exc:
                    raise ValueError(
                        f"{PRIORITY_SPECIES_FILE}: line {line_no}: invalid inat_id '{inat_raw}'"
                    ) from exc
            rows.append({
                "scientific_name": sci,
                "taxon_group": group,
                "source": (row.get("source") or "").strip(),
                "reason": (row.get("reason") or "").strip(),
                "inat_id": inat_id,
                "gbif_id": (row.get("gbif_id") or "").strip(),
                "common_name": (row.get("common_name") or "").strip(),
            })
    return rows


def fetch_priority_species(existing: dict, cfg: dict, delay: float,
                           all_names: bool, limit: int = 0,
                           save_every: int = 200,
                           new_only: bool = False) -> int:
    """Fetch reviewed priority species by iNat ID or scientific name."""
    rows = load_priority_species()
    print(f"\n{'='*60}")
    print("Priority species")

    if not rows:
        print("  No priority species file found")
        return 0

    allowed_groups = {g.get("name") for g in cfg.get("taxon_groups", [])}
    rows = [r for r in rows if r["taxon_group"] in allowed_groups]
    missing = [
        r for r in rows
        if r["scientific_name"] not in existing
        or existing[r["scientific_name"]].get("inat_id") is None
    ]
    if new_only:
        missing = [r for r in missing if r["scientific_name"] not in existing]
    if limit:
        missing = missing[:limit]

    print(f"  Reviewed rows: {len(rows)}")
    print(f"  Need to fetch: {len(missing)}")
    if not missing:
        return 0

    found_taxa: list[tuple[dict, int]] = []
    not_found: list[str] = []
    for row in missing:
        if is_shutting_down():
            break
        if row.get("inat_id"):
            found_taxa.append((row, row["inat_id"]))
        else:
            result = _search_taxon(row["scientific_name"], delay)
            if result:
                found_taxa.append((row, result.get("id")))
            else:
                not_found.append(row["scientific_name"])
            time.sleep(delay)

    if not_found:
        print(f"  Not found: {', '.join(not_found)}")
    if not found_taxa:
        return 0

    new_count = 0
    batches = [
        found_taxa[i:i + TAXA_BATCH_SIZE]
        for i in range(0, len(found_taxa), TAXA_BATCH_SIZE)
    ]
    for batch_idx, batch in enumerate(batches):
        if is_shutting_down():
            break
        batch_ids = [tid for _, tid in batch]
        rows_by_id = {tid: row for row, tid in batch}
        results = fetch_taxa_batch(batch_ids, all_names, delay)
        for result in results:
            tid = result.get("id")
            row = rows_by_id.get(tid)
            if not row:
                continue
            sci = row["scientific_name"]
            if sci in existing and existing[sci].get("inat_id") is not None:
                continue
            if result.get("rank") != "species":
                continue
            record = extract_record(result, row["taxon_group"], sound_count=0)
            if row.get("common_name"):
                record["preferred_common_name"] = row["common_name"]
                record.setdefault("common_names", {})["en"] = row["common_name"]
            record["priority_source"] = row.get("source", "")
            record["priority_reason"] = row.get("reason", "")
            if row.get("gbif_id"):
                record["priority_gbif_id"] = row["gbif_id"]
            existing[sci] = record
            new_count += 1

        print(
            f"  Detail batch {batch_idx + 1}/{len(batches)} — "
            f"{new_count} new records",
            flush=True,
        )
        if new_count > 0 and new_count % save_every < TAXA_BATCH_SIZE:
            save_json(existing, OUTPUT_FILE)
        time.sleep(delay)

    save_json(existing, OUTPUT_FILE)
    print(f"  Priority species complete: {new_count} species added")
    return new_count


# ── Observation photo fallback ─────────────────────────────────────────

INAT_OBS_URL = "https://api.inaturalist.org/v1/observations"
OBS_PHOTO_LOOKUP_KEY = "obs_photo_lookup"


def _obs_photo_lookup_state(record: dict) -> dict:
    """Return persisted observation-photo lookup state for a species."""
    state = record.get(OBS_PHOTO_LOOKUP_KEY)
    return state if isinstance(state, dict) else {}


def _obs_photo_checked(record: dict) -> bool:
    """Return True if observation-photo fallback was already attempted."""
    return bool(_obs_photo_lookup_state(record).get("checked_at"))


def _mark_obs_photo_lookup(record: dict, status: str):
    """Persist the outcome of an observation-photo lookup attempt."""
    record[OBS_PHOTO_LOOKUP_KEY] = {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_obs_photo(inat_id: int, delay: float) -> dict | None:
    """Find the best CC-licensed photo for a taxon via the observations API.

    Queries research-grade observations sorted by votes (most-faved first)
    and returns the first photo with an acceptable license.
    Returns {url, attribution, license} or None.
    """
    url = (
        f"{INAT_OBS_URL}?taxon_id={inat_id}"
        f"&photos=true&photo_licensed=true"
        f"&quality_grade=research&order_by=votes&per_page=5"
    )
    for attempt in range(3):
        data = _api_get(url)
        if data is not None:
            break
        if attempt < 2:
            time.sleep(delay * (attempt + 1))
    else:
        return None

    for obs in data.get("results", []):
        for photo in obs.get("photos", []):
            lic = photo.get("license_code", "")
            if lic and lic in ACCEPTABLE_LICENSES:
                return {
                    "url": photo_url_large(photo.get("url", "")),
                    "attribution": photo.get("attribution", ""),
                    "license": lic,
                }

    return None


def fetch_obs_photos(existing: dict, delay: float,
                     limit: int = 0,
                     refresh: bool = False) -> int:
    """Find CC-licensed observation photos for species without one.

    Checks species whose default taxon photo is missing or not CC-licensed.
    Stores result in the `obs_photo` field of each species record.

    Returns count of photos found.
    """
    print(f"\n{'='*60}")
    print("Observation photo fallback")

    need_photo = []
    already_checked = 0
    for sci, rec in existing.items():
        inat_id = rec.get("inat_id")
        if not inat_id:
            continue
        if rec.get("obs_photo"):
            continue  # already have one
        if not refresh and _obs_photo_checked(rec):
            already_checked += 1
            continue
        img_lic = rec.get("image_license", "")
        if rec.get("image_url") and img_lic in ACCEPTABLE_LICENSES:
            continue  # default taxon photo is CC
        need_photo.append((sci, inat_id))

    print(f"  Species needing observation photo lookup: {len(need_photo)}")
    if already_checked:
        print(f"  Skipping {already_checked} species already checked earlier")

    if not need_photo:
        print("  Nothing to do!")
        return 0

    if limit:
        need_photo = need_photo[:limit]
        print(f"  Limited to {limit}")

    found = 0
    for i, (sci, inat_id) in enumerate(need_photo):
        if is_shutting_down():
            break

        result = _fetch_obs_photo(inat_id, delay)
        if result and result.get("url"):
            existing[sci]["obs_photo"] = result
            _mark_obs_photo_lookup(existing[sci], "found")
            found += 1
        else:
            _mark_obs_photo_lookup(existing[sci], "not_found")

        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(need_photo)} checked, {found} found")
            save_json(existing, OUTPUT_FILE)

        time.sleep(delay)

    save_json(existing, OUTPUT_FILE)
    print(f"  Found {found} CC-licensed photos from observations")
    return found


def _print_dry_run_scope(group_name: str, scope: dict,
                         existing: dict, delay: float) -> None:
    """Print dry-run counts for a configured taxon scope."""
    sounds_only = scope.get("sounds_only", True)
    min_obs = scope.get("min_observations", 0)
    min_total_obs = scope.get("min_total_observations", 0)
    taxon_id = scope["taxon_id"]
    label_name = group_name
    if scope.get("name") and scope.get("name") != group_name:
        label_name = f"{group_name}/{scope['name']}"

    if sounds_only:
        url = (
            f"{SPECIES_COUNTS_URL}"
            f"?taxon_id={taxon_id}&sounds=true&per_page=1"
        )
        data = _api_get(url)
        total_sounds = data["total_results"] if data else "?"
        label = f" (≥{min_obs} sound obs)" if min_obs else ""
        desc = f"{total_sounds} species with sounds{label}"
        if min_total_obs:
            time.sleep(delay)
            url2 = (
                f"{SPECIES_COUNTS_URL}"
                f"?taxon_id={taxon_id}&per_page=1"
            )
            data2 = _api_get(url2)
            total_obs = data2["total_results"] if data2 else "?"
            desc += f"; {total_obs} species with observations (include ≥{min_total_obs})"
    else:
        url = (
            f"{TAXA_URL}?is_active=true&rank=species"
            f"&taxon_id={taxon_id}&per_page=1"
        )
        data = _api_get(url)
        total = data["total_results"] if data else "?"
        time.sleep(delay)
        url2 = (
            f"{SPECIES_COUNTS_URL}"
            f"?taxon_id={taxon_id}&sounds=true&per_page=1"
        )
        data2 = _api_get(url2)
        total_sounds = data2["total_results"] if data2 else "?"
        desc = f"{total} total species ({total_sounds} with sounds)"

    existing_in_group = sum(
        1 for v in existing.values()
        if v.get("taxon_group") == group_name
        and v.get("inat_id") is not None
    )
    print(f"  {label_name}: {desc}, {existing_in_group} already fetched")
    time.sleep(delay)


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    groups = cfg.get("taxon_groups", [])
    group_names = [g["name"] for g in groups]

    parser = argparse.ArgumentParser(
        description="Fetch iNaturalist taxonomy for species with sound observations"
    )
    parser.add_argument(
        "--group", type=str, default="",
        help=f"Fetch only this group (choices: {', '.join(group_names)})"
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Max new species to fetch per group (0 = all)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show species counts with sound observations per group"
    )
    parser.add_argument(
        "--new-only", action="store_true",
        help="Only fetch new species; skip count/photo refresh work"
    )
    parser.add_argument(
        "--save-every", type=int, default=0,
        help="Save progress every N new species (default: from config.yml)"
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Bypass cached Phase 1 data and re-fetch from API"
    )
    parser.add_argument(
        "--skip-avilist", action="store_true",
        help="Skip AviList reconciliation phase"
    )
    parser.add_argument(
        "--skip-priority", action="store_true",
        help="Skip reviewed priority species phase"
    )
    parser.add_argument(
        "--priority-only", action="store_true",
        help="Only run reviewed priority species phase"
    )
    parser.add_argument(
        "--avilist-only", action="store_true",
        help="Only run AviList reconciliation (skip group fetching)"
    )
    parser.add_argument(
        "--obs-photos-only", action="store_true",
        help="Only run observation photo fallback (skip group fetching)"
    )
    parser.add_argument(
        "--skip-obs-photos", action="store_true",
        help="Skip observation photo fallback phase"
    )
    parser.add_argument(
        "--refresh-obs-photos", action="store_true",
        help="Ignore cached observation photo lookup results and recheck species"
    )
    args = parser.parse_args()

    # Filter groups
    if args.group:
        groups = [g for g in groups if g["name"].lower() == args.group.lower()]
        if not groups:
            print(f"ERROR: Unknown group '{args.group}'. Available: {', '.join(group_names)}")
            raise SystemExit(1)

    existing = load_json(OUTPUT_FILE)
    print(f"Loaded {len(existing)} existing species records")
    print(f"Groups to fetch: {', '.join(g['name'] for g in groups)}")

    if args.dry_run:
        delay = cfg.get("inat", {}).get("request_delay", 1.1)
        for g in groups:
            _print_dry_run_scope(g["name"], g, existing, delay)
            for sub in g.get("subtaxa", []) or []:
                _print_dry_run_scope(g["name"], sub, existing, delay)
        priority = load_priority_species()
        priority_missing = [
            r for r in priority
            if r["scientific_name"] not in existing
            or existing[r["scientific_name"]].get("inat_id") is None
        ]
        print(f"  Priority species: {len(priority)} reviewed, {len(priority_missing)} need fetch")
        return

    inat_cfg = cfg.get("inat", {})
    if not args.save_every:
        args.save_every = inat_cfg.get("save_every", 500)
    total_new = 0

    if not args.avilist_only and not args.obs_photos_only and not args.priority_only:
        for group in groups:
            if is_shutting_down():
                break
            new = fetch_group(group, existing, cfg, limit=args.limit,
                              save_every=args.save_every, refresh=args.refresh,
                              new_only=args.new_only)
            total_new += new

    # Priority species — reviewed exceptions / issue-driven additions
    if (not args.skip_priority and not args.avilist_only
            and not args.obs_photos_only and not is_shutting_down()):
        priority_new = fetch_priority_species(
            existing, cfg,
            delay=inat_cfg.get("request_delay", 1.1),
            all_names=inat_cfg.get("all_names", True),
            limit=args.limit,
            save_every=args.save_every,
            new_only=args.new_only,
        )
        total_new += priority_new

    # AviList reconciliation — find AviList species missing from iNat data
    if (not args.skip_avilist and not args.obs_photos_only and not args.priority_only
            and not is_shutting_down()):
        avilist_new = reconcile_avilist(
            existing, cfg,
            delay=inat_cfg.get("request_delay", 1.1),
            all_names=inat_cfg.get("all_names", True),
            limit=args.limit,
            save_every=args.save_every,
        )
        total_new += avilist_new

    # Observation photo fallback — CC-licensed photos from observations
    if (not args.skip_obs_photos and not args.new_only
            and not args.priority_only and not is_shutting_down()):
        obs_found = fetch_obs_photos(
            existing,
            delay=inat_cfg.get("request_delay", 1.1),
            limit=args.limit,
            refresh=args.refresh_obs_photos,
        )

    # Final stats
    print(f"\n{'='*60}")
    print(f"All done! {total_new} new species added.")
    print(f"Total records in {OUTPUT_FILE.name}: {len(existing)}")
    for g in groups:
        count = sum(
            1 for v in existing.values()
            if v.get("taxon_group") == g["name"]
            and v.get("inat_id") is not None
        )
        print(f"  {g['name']}: {count}")


if __name__ == "__main__":
    main()
