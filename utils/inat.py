#!/usr/bin/env python3
"""
Fetch the full iNaturalist taxonomy for configured taxon groups.

Paginates through the iNat taxa API to download every active species
for each group defined in config.yml (Aves, Mammalia, Insecta, etc.).
Results are stored per-group and merged into a single output file.

Output: raw_data/inat_data.json (incremental, resumable)

Usage:
    python -m utils.inat [--group NAME] [--limit N] [--dry-run]

iNat API: https://api.inaturalist.org/v1/taxa
  ?is_active=true&rank=species&all_names=true
  &order=desc&order_by=observations_count
  &taxon_id={id}&per_page=200&page={n}
"""

import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from utils.config import load_config

# Paths
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = ROOT / "raw_data" / "inat_data.json"

USER_AGENT = "species-data-collector/1.0 (https://github.com/birdnet-team/species-data)"


def load_existing_data() -> dict:
    """Load already-fetched iNat data."""
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    """Save iNat data to disk."""
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def photo_url_large(url: str) -> str:
    """Convert iNat photo URL from square/medium to large."""
    if not url:
        return ""
    return url.replace("/square.", "/large.").replace("/medium.", "/large.")


def fetch_page(base_url: str, taxon_id: int, page: int, per_page: int,
               all_names: bool) -> dict | None:
    """Fetch one page of species from the iNat taxa API."""
    url = (
        f"{base_url}"
        f"?is_active=true&rank=species"
        f"&all_names={'true' if all_names else 'false'}"
        f"&order=desc&order_by=observations_count"
        f"&taxon_id={taxon_id}"
        f"&per_page={per_page}&page={page}"
    )
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"  ERROR on page {page}: {e}")
        return None


def extract_record(result: dict, group_name: str) -> dict:
    """Extract relevant fields from an iNat API result."""
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
        "preferred_common_name": result.get("preferred_common_name", ""),
    }


def fetch_group(group: dict, existing: dict, cfg: dict,
                limit: int = 0, save_every: int = 500) -> int:
    """Fetch species for one taxon group. Returns count of new records.

    Results are sorted by observation count (descending) by the API.
    The group's 'fraction' setting (0.0–1.0) controls what share of
    species to fetch — e.g. 0.1 fetches the top 10% most-observed.
    """
    group_name = group["name"]
    taxon_id = group["taxon_id"]
    fraction = group.get("fraction", 1.0)
    inat_cfg = cfg.get("inat", {})
    base_url = inat_cfg.get("base_url", "https://api.inaturalist.org/v1/taxa")
    per_page = min(inat_cfg.get("per_page", 200), 200)  # iNat API caps at 200
    all_names = inat_cfg.get("all_names", True)
    delay = inat_cfg.get("request_delay", 1.1)

    print(f"\n{'='*60}")
    print(f"Fetching {group_name} (taxon_id={taxon_id}, fraction={fraction})...")

    # First request to get total
    data = fetch_page(base_url, taxon_id, page=1, per_page=per_page,
                      all_names=all_names)
    if not data:
        print(f"  ERROR: Could not fetch first page for {group_name}")
        return 0

    total = data["total_results"]
    # Detect actual per_page returned by API (it may silently cap)
    actual_per_page = len(data.get("results", []))
    if actual_per_page and actual_per_page < per_page:
        per_page = actual_per_page

    # Apply fraction cap — only fetch top N% of species (sorted by obs count)
    target = int(total * fraction) if fraction < 1.0 else total
    target_pages = (target + per_page - 1) // per_page
    total_pages = (total + per_page - 1) // per_page
    print(f"  Total species on iNat: {total} (target: {target}, ~{target_pages} pages of {per_page})")
    if fraction < 1.0:
        print(f"  Fraction {fraction} → fetching top {target} species")

    # Count how many we already have for this group
    existing_in_group = sum(
        1 for v in existing.values()
        if v.get("taxon_group") == group_name and v.get("inat_id") is not None
    )
    print(f"  Already have: {existing_in_group} from previous runs")

    new_count = 0
    skipped = 0
    seen_total = 0  # actual species seen across all pages
    page = 1

    while True:
        if page > 1:
            data = fetch_page(base_url, taxon_id, page=page, per_page=per_page,
                              all_names=all_names)
            if not data:
                print(f"  ERROR on page {page}, retrying once...")
                time.sleep(delay * 3)
                data = fetch_page(base_url, taxon_id, page=page,
                                  per_page=per_page, all_names=all_names)
                if not data:
                    print(f"  FAILED page {page}, stopping this group")
                    break

        results = data.get("results", [])
        if not results:
            break

        for result in results:
            sci_name = result.get("name", "")
            if not sci_name:
                continue

            # Skip if already fetched
            if sci_name in existing and existing[sci_name].get("inat_id") is not None:
                skipped += 1
                continue

            record = extract_record(result, group_name)
            existing[sci_name] = record
            new_count += 1

            if limit and new_count >= limit:
                break

        seen_total += len(results)
        print(
            f"  Page {page}/{target_pages} — "
            f"{len(results)} species, "
            f"{new_count} new, {skipped} skipped, "
            f"{seen_total}/{target} seen",
            flush=True,
        )

        if new_count > 0 and new_count % save_every < per_page:
            save_data(existing)

        if limit and new_count >= limit:
            print(f"  Reached limit of {limit}")
            break

        if seen_total >= target:
            if fraction < 1.0:
                print(f"  Reached fraction cap ({target}/{total} species)")
            break

        if page >= total_pages:
            break

        page += 1
        time.sleep(delay)

    save_data(existing)
    print(f"  Done {group_name}: {new_count} new species added")
    return new_count


def main():
    cfg = load_config()
    groups = cfg.get("taxon_groups", [])
    group_names = [g["name"] for g in groups]

    parser = argparse.ArgumentParser(
        description="Fetch iNaturalist taxonomy for configured taxon groups"
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
        help="Show what would be fetched without fetching"
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

    existing = load_existing_data()
    print(f"Loaded {len(existing)} existing species records")
    print(f"Groups to fetch: {', '.join(g['name'] for g in groups)}")

    if args.dry_run:
        inat_cfg = cfg.get("inat", {})
        base_url = inat_cfg.get("base_url", "https://api.inaturalist.org/v1/taxa")
        per_page = inat_cfg.get("per_page", 200)
        for g in groups:
            frac = g.get("fraction", 1.0)
            data = fetch_page(base_url, g["taxon_id"], page=1, per_page=1,
                              all_names=False)
            total = data["total_results"] if data else "?"
            target = int(total * frac) if isinstance(total, int) and frac < 1.0 else total
            existing_in_group = sum(
                1 for v in existing.values()
                if v.get("taxon_group") == g["name"]
                and v.get("inat_id") is not None
            )
            label = f" (top {frac:.0%})" if frac < 1.0 else ""
            print(f"  {g['name']}: {total} on iNat{label} → target {target}, {existing_in_group} already fetched")
        return

    total_new = 0
    for group in groups:
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