#!/usr/bin/env python3
"""
Fetch species descriptions and images from eBird species pages.

Scrapes og:description and og:image meta tags from eBird species pages.
Only applies to bird species that have an eBird species code (from AviList
cross-referenced via inat_data.json).

Output: raw_data/ebird_data.json (incremental, resumable)

Usage:
    python -m utils.ebird [--limit N] [--dry-run]

eBird image URL pattern:
  https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{id}/{size}
  Sizes: 320, 480, 640, 900, 1200, 1800, 2400 (all return 200 OK)
  The og:image tag uses /900 by default. We store the base URL without
  the size suffix so consumers can choose their preferred resolution.
"""

import argparse
import csv
import json
import os
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError, URLError

from tqdm import tqdm

from utils.config import load_config

# Paths
ROOT = Path(__file__).resolve().parent.parent
INAT_DATA = ROOT / "raw_data" / "inat_data.json"
AVILIST_CSV = ROOT / "raw_data"  # dir; filename from config
OUTPUT_FILE = ROOT / "raw_data" / "ebird_data.json"

# Reusable opener with cookie support (thread-local for safety)
_local = threading.local()

def _get_opener():
    """Get a thread-local HTTP opener with cookie support."""
    if not hasattr(_local, "opener"):
        jar = CookieJar()
        _local.opener = build_opener(HTTPCookieProcessor(jar))
    return _local.opener

# Graceful shutdown flag
_shutdown = False

def _handle_sigint(sig, frame):
    global _shutdown
    if _shutdown:
        raise SystemExit(1)
    _shutdown = True
    tqdm.write("\n⏎ Interrupt received — finishing current request and saving...")

signal.signal(signal.SIGINT, _handle_sigint)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket rate limiter (thread-safe)."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval

_rate = RateLimiter(5)  # default; overwritten in main()


def load_species_with_ebird_codes() -> dict[str, str]:
    """Load species that have an eBird code.

    First tries inat_data.json (has ebird_code if species is a bird),
    then falls back to reading the AviList CSV directly.

    Returns dict of scientific_name -> ebird_code.
    """
    species = {}

    # From inat_data.json
    if INAT_DATA.exists():
        with open(INAT_DATA, encoding="utf-8") as f:
            inat_data = json.load(f)
        for sci_name, record in inat_data.items():
            if record.get("inat_id") is None:
                continue
            ebird_code = record.get("ebird_code", "").strip()
            if ebird_code:
                species[sci_name] = ebird_code

    # Also try AviList CSV for any codes not in inat_data
    cfg = load_config()
    csv_name = cfg.get("avilist", {}).get("csv_file", "")
    csv_path = ROOT / "raw_data" / csv_name if csv_name else None
    if csv_path and csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                if row.get("Taxon_rank") != "species":
                    continue
                sci = row.get("Scientific_name", "").strip()
                code = row.get("Species_code_Cornell_Lab", "").strip()
                if sci and code and sci not in species:
                    species[sci] = code

    if not species:
        print("ERROR: No eBird codes found. Run utils/inat.py first or ensure AviList CSV exists.")
        raise SystemExit(1)

    return species


def load_existing_data() -> dict:
    """Load already-fetched eBird data."""
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    """Save eBird data to disk (atomic write)."""
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUTPUT_FILE)


def _extract_og_tag(html: str, tag: str) -> str | None:
    """Extract an OpenGraph meta tag value from HTML."""
    # property="og:X" content="..."
    match = re.search(
        rf'<meta\s+property="og:{tag}"\s+content="([^"]*)"',
        html, re.IGNORECASE,
    )
    if not match:
        # content="..." property="og:X"
        match = re.search(
            rf'<meta\s+content="([^"]*)"\s+property="og:{tag}"',
            html, re.IGNORECASE,
        )
    return match.group(1).strip() if match else None


def _image_base_url(og_image_url: str) -> str:
    """Strip the size suffix from an eBird CDN image URL.

    Input:  https://cdn.download.ams.birds.cornell.edu/api/v2/asset/46409481/900
    Output: https://cdn.download.ams.birds.cornell.edu/api/v2/asset/46409481
    """
    if not og_image_url:
        return ""
    # Remove trailing /NNN size
    return re.sub(r'/\d+$', '', og_image_url)


def _fetch_ebird_html(opener, url: str) -> str | None:
    """Make a rate-limited request to eBird with retry on 429.

    Returns HTML string on success, None on rate-limit exhaustion.
    Raises HTTPError/URLError on other failures.
    """
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    for attempt in range(4):
        _rate.acquire()
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code == 429:
                wait = min(2 ** attempt * 2, 60)
                time.sleep(wait)
                continue
            raise
    return None


def _code_variants(ebird_code: str) -> list[str]:
    """Generate fallback code variants for outdated versioned codes.

    AviList sometimes has versioned codes (e.g. virrai2) that 404 on eBird.
    Try: original -> strip version to base+1 -> base code.
    """
    variants = [ebird_code]
    # If code ends with a digit ≥ 2, try version 1 and base
    m = re.match(r'^([a-z]+?)(\d+)$', ebird_code)
    if m:
        base, version = m.group(1), int(m.group(2))
        if version >= 2:
            variants.append(f"{base}1")
        variants.append(base)
    return variants


def fetch_ebird_page(ebird_code: str, base_url: str) -> dict:
    """Fetch og:description and og:image from an eBird species page.

    Falls back to alternative code variants on 404.
    """
    opener = _get_opener()

    for code in _code_variants(ebird_code):
        url = base_url + quote(code)
        try:
            html = _fetch_ebird_html(opener, url)
        except HTTPError as e:
            if e.code == 404:
                continue  # try next variant
            return {"error": str(e)}
        except (URLError, TimeoutError) as e:
            return {"error": str(e)}

        if html is None:
            return {"error": "rate_limited_after_retries"}

        description = _extract_og_tag(html, "description")
        image_url = _extract_og_tag(html, "image")
        image_alt = _extract_og_tag(html, "image:alt")

        return {
            "description": description,
            "image_url": _image_base_url(image_url or ""),
            "image_attribution": image_alt or "",
        }

    return {"error": f"404_all_variants({ebird_code})"}


def main():
    cfg = load_config()
    ebird_cfg = cfg.get("ebird", {})
    base_url = ebird_cfg.get("base_url", "https://ebird.org/species/")
    default_workers = ebird_cfg.get("workers", 4)
    default_rps = ebird_cfg.get("rps", 5)

    parser = argparse.ArgumentParser(description="Fetch eBird species descriptions and images")
    parser.add_argument("--limit", type=int, default=0, help="Max species to fetch (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without fetching")
    parser.add_argument("--workers", type=int, default=default_workers,
                        help=f"Number of parallel fetchers (default: {default_workers})")
    parser.add_argument("--rps", type=float, default=default_rps,
                        help=f"Max requests per second (default: {default_rps})")
    args = parser.parse_args()

    # Set global rate limiter
    global _rate
    _rate = RateLimiter(args.rps)

    print("Loading species with eBird codes...")
    species = load_species_with_ebird_codes()
    print(f"  Found {len(species)} species with eBird codes")

    existing = load_existing_data()
    print(f"  Already have eBird data for {len(existing)} species")

    to_fetch = [(sci, code) for sci, code in species.items() if sci not in existing]
    if args.limit:
        to_fetch = to_fetch[:args.limit]

    print(f"  Will fetch {len(to_fetch)} species from eBird ({args.workers} workers, {args.rps} rps)")

    if args.dry_run:
        for sci, code in to_fetch[:20]:
            print(f"    {sci} -> {base_url}{code}")
        if len(to_fetch) > 20:
            print(f"    ... and {len(to_fetch) - 20} more")
        return

    def _fetch_one(item):
        sci_name, ebird_code = item
        result = fetch_ebird_page(ebird_code, base_url)
        if result.get("error"):
            return sci_name, {
                "ebird_code": ebird_code,
                "description": None,
                "image_url": "",
                "image_attribution": "",
                "error": result["error"],
            }
        elif result.get("description"):
            return sci_name, {
                "ebird_code": ebird_code,
                "description": result["description"],
                "image_url": result["image_url"],
                "image_attribution": result["image_attribution"],
            }
        else:
            return sci_name, {
                "ebird_code": ebird_code,
                "description": None,
                "image_url": result.get("image_url", ""),
                "image_attribution": result.get("image_attribution", ""),
                "error": "no_description",
            }

    success = 0
    pbar = tqdm(total=len(to_fetch), desc="eBird", unit="sp")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for item in to_fetch:
            if _shutdown:
                break
            futures[pool.submit(_fetch_one, item)] = item[0]

        for future in as_completed(futures):
            sci_name = futures[future]
            try:
                _, record = future.result()
                existing[sci_name] = record
                if "error" not in record:
                    success += 1
                    pbar.set_postfix_str(sci_name, refresh=False)
                else:
                    err = record.get("error", "")
                    if err != "no_description":
                        tqdm.write(f"  ERROR {sci_name}: {err}")
            except Exception as exc:
                tqdm.write(f"  EXCEPTION {sci_name}: {exc}")
                existing[sci_name] = {"error": str(exc)}
            pbar.update(1)
            save_data(existing)

            if _shutdown:
                for f in futures:
                    f.cancel()
                break

    pbar.close()
    print(f"\nDone! Fetched {pbar.n} species, {success} with descriptions.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries")


if __name__ == "__main__":
    main()