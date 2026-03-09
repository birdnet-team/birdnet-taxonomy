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

Output: raw_data/inat_data.json (incremental, resumable)

Usage:
    python -m collectors.inat [--group NAME] [--limit N] [--dry-run]
"""

import argparse
import json
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT, setup_shutdown, is_shutting_down,
    load_json, save_json,
)

OUTPUT_FILE = RAW_DIR / "inat_data.json"
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
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"  ERROR: {e} — {url[:120]}")
        return None


def photo_url_large(url: str) -> str:
    """Convert iNat photo URL from square/medium to large."""
    if not url:
        return ""
    return url.replace("/square.", "/large.").replace("/medium.", "/large.")


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
            species.append({
                "id": taxon.get("id"),
                "name": taxon.get("name", ""),
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
                limit: int = 0, save_every: int = 500) -> int:
    """Fetch species for one taxon group. Returns count of new records.

    If sounds_only is true: bulk-query species_counts for species with sounds,
    then batch-fetch full taxon details.
    If sounds_only is false: paginate all species via taxa API, then enrich
    with sound counts.
    """
    group_name = group["name"]
    taxon_id = group["taxon_id"]
    sounds_only = group.get("sounds_only", True)
    min_obs = group.get("min_observations", 0)
    inat_cfg = cfg.get("inat", {})
    all_names = inat_cfg.get("all_names", True)
    delay = inat_cfg.get("request_delay", 1.1)

    print(f"\n{'='*60}")

    if sounds_only:
        return _fetch_sounds_only(group_name, taxon_id, min_obs,
                                  all_names, delay, existing, limit, save_every)
    else:
        return _fetch_all_species(group_name, taxon_id,
                                  all_names, delay, existing, limit, save_every)


def _fetch_sounds_only(group_name: str, taxon_id: int, min_obs: int,
                       all_names: bool, delay: float, existing: dict,
                       limit: int, save_every: int) -> int:
    """Fetch only species with sound observations (sounds_only=true groups)."""
    min_obs_label = f"≥{min_obs} sound obs" if min_obs else "all with sounds"
    print(f"Fetching {group_name} (taxon_id={taxon_id}, {min_obs_label})")

    # Phase 1: get species with sound observations
    print("  Phase 1: Fetching sound observation counts...")
    sound_species = fetch_sound_species(taxon_id, min_obs, delay)
    print(f"  Found {len(sound_species)} species with sound observations")

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
        updated = _update_sound_counts(existing, sound_lookup, group_name)
        if updated:
            save_json(existing, OUTPUT_FILE)
            print(f"  Updated sound counts for {updated} existing species")
        return 0

    # Phase 2: batch-fetch full taxon details
    print("  Phase 2: Fetching full taxon details...")
    new_count = _batch_fetch_taxa(to_fetch, group_name, all_names, delay,
                                  existing, save_every)

    updated = _update_sound_counts(existing, sound_lookup, group_name)
    save_json(existing, OUTPUT_FILE)
    print(f"  Done {group_name}: {new_count} new species added")
    if updated:
        print(f"  Updated sound counts for {updated} existing species")
    return new_count


def _fetch_all_species(group_name: str, taxon_id: int,
                       all_names: bool, delay: float, existing: dict,
                       limit: int, save_every: int) -> int:
    """Fetch ALL species for a group (sounds_only=false), enrich with sound counts."""
    print(f"Fetching {group_name} (taxon_id={taxon_id}, all species)")

    existing_in_group = sum(
        1 for v in existing.values()
        if v.get("taxon_group") == group_name and v.get("inat_id") is not None
    )
    print(f"  Already have: {existing_in_group} from previous runs")

    # Phase 1: paginate all species from taxa API
    print("  Phase 1: Fetching all species from taxa API...")
    all_results = fetch_all_species(taxon_id, per_page=200, all_names=all_names,
                                    delay=delay)
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

    # Phase 2: enrich with sound observation counts
    print("  Phase 2: Fetching sound observation counts...")
    sound_species = fetch_sound_species(taxon_id, min_obs=0, delay=delay)
    sound_lookup = {s["id"]: s["sound_count"] for s in sound_species}
    updated = _update_sound_counts(existing, sound_lookup, group_name)

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
        "--save-every", type=int, default=500,
        help="Save progress every N new species"
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
            sounds_only = g.get("sounds_only", True)
            min_obs = g.get("min_observations", 0)

            if sounds_only:
                url = (
                    f"{SPECIES_COUNTS_URL}"
                    f"?taxon_id={g['taxon_id']}&sounds=true&per_page=1"
                )
                data = _api_get(url)
                total_sounds = data["total_results"] if data else "?"
                label = f" (≥{min_obs} sound obs)" if min_obs else ""
                desc = f"{total_sounds} species with sounds{label}"
            else:
                url = (
                    f"{TAXA_URL}?is_active=true&rank=species"
                    f"&taxon_id={g['taxon_id']}&per_page=1"
                )
                data = _api_get(url)
                total = data["total_results"] if data else "?"
                # Also check sound species count
                time.sleep(delay)
                url2 = (
                    f"{SPECIES_COUNTS_URL}"
                    f"?taxon_id={g['taxon_id']}&sounds=true&per_page=1"
                )
                data2 = _api_get(url2)
                total_sounds = data2["total_results"] if data2 else "?"
                desc = f"{total} total species ({total_sounds} with sounds)"

            existing_in_group = sum(
                1 for v in existing.values()
                if v.get("taxon_group") == g["name"]
                and v.get("inat_id") is not None
            )
            print(f"  {g['name']}: {desc}, {existing_in_group} already fetched")
            time.sleep(delay)
        return

    total_new = 0
    for group in groups:
        if is_shutting_down():
            break
        new = fetch_group(group, existing, cfg, limit=args.limit,
                          save_every=args.save_every)
        total_new += new

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