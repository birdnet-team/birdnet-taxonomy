#!/usr/bin/env python3
"""
Download the Avicommons bird photo index.

Avicommons (https://avicommons.org) is a hand-picked set of cropped,
Creative-Commons-licensed bird photos keyed by eBird species code. The build
references the matching photo next to the species' own image; it never
replaces it.

Avicommons asks consumers to download its JSON once (at build time), not per
page load, so this collector fetches the release file configured under
`avicommons.url` and the build reads the local copy.

Output: raw_data/avicommons_data.json

Usage:
    python -m collectors.avicommons [--force] [--dry-run]
"""

import argparse
import json
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import load_config
from collectors._common import RAW_DIR, USER_AGENT, load_json, save_json

OUTPUT_FILE = RAW_DIR / "avicommons_data.json"
REQUIRED_KEYS = ("code", "sciName", "key", "license", "by")


def download_index(url: str) -> list[dict]:
    """Download and sanity-check the Avicommons full JSON index."""
    print(f"  Downloading {url}...")
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=120) as resp:
            photos = json.loads(resp.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  ERROR: {e}")
        raise SystemExit(1)
    if not isinstance(photos, list) or not photos:
        print("  ERROR: expected a non-empty list (use the full JSON, not the lite version)")
        raise SystemExit(1)
    missing = [k for k in REQUIRED_KEYS if k not in photos[0]]
    if missing:
        print(f"  ERROR: Avicommons entries lack keys: {', '.join(missing)}")
        raise SystemExit(1)
    return photos


def main():
    cfg = load_config().get("avicommons", {})
    url = cfg.get("url", "")
    if not url:
        print("ERROR: avicommons.url must be set in config.yml")
        raise SystemExit(1)

    parser = argparse.ArgumentParser(description="Download the Avicommons photo index")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if the configured release is cached")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded without fetching")
    args = parser.parse_args()

    existing = load_json(OUTPUT_FILE)
    if existing.get("url") == url and existing.get("photos") and not args.force:
        print(f"Avicommons index already cached: {len(existing['photos'])} photos "
              f"from {url} (downloaded {existing.get('downloaded', '?')})")
        print("  Use --force to re-download.")
        return

    if args.dry_run:
        print(f"Would download {url} → {OUTPUT_FILE.name}")
        return

    photos = download_index(url)
    save_json({"url": url, "downloaded": date.today().isoformat(), "photos": photos},
              OUTPUT_FILE)
    print(f"  Saved {len(photos)} photos to {OUTPUT_FILE.name}")
    print("Done!")


if __name__ == "__main__":
    main()
