#!/usr/bin/env python3
"""
Fetch species descriptions, images, and common names from eBird.

Phase 1 (scraper): Scrapes og:description and og:image meta tags from eBird
  species pages. Only applies to bird species that have an eBird species code.

Phase 2 (names): Downloads the eBird taxonomy CSV for 62 locales to collect
  common names in all available languages. Outputs ebird_names.json.

Output:
  - raw_data/ebird_data.json     (Phase 1: descriptions + images, incremental)
  - raw_data/ebird_names.json    (Phase 2: common names by eBird code)
  - raw_data/ebird_taxonomy.json (Phase 2: eBird code → scientific name)

Phase 2 downloads are cached per eBird taxonomy version, so a new eBird
release is picked up on the next run.

Usage:
    python -m collectors.ebird [--limit N] [--dry-run]
    python -m collectors.ebird --names-only
"""

import argparse
import csv
import io
import json
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError, URLError

from tqdm import tqdm

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT, setup_shutdown, is_shutting_down,
    RateLimiter, load_json, save_json,
    cache_key, cache_get, cache_put,
)

INAT_DATA = RAW_DIR / "inat_data.json"
OUTPUT_FILE = RAW_DIR / "ebird_data.json"
NAMES_FILE = RAW_DIR / "ebird_names.json"
TAXONOMY_FILE = RAW_DIR / "ebird_taxonomy.json"
TAXONOMY_JSON = RAW_DIR / "taxonomy.json"

EBIRD_TAXONOMY_URL = "https://api.ebird.org/v2/ref/taxonomy/ebird"
EBIRD_VERSIONS_URL = "https://api.ebird.org/v2/ref/taxonomy/versions"

# All known eBird locales with real translations.
# (ebird_locale, canonical_locale) — canonical is stored in common_names.
EBIRD_LOCALES: list[tuple[str, str]] = [
    ("af", "af"), ("ar", "ar"), ("bg", "bg"), ("bn", "bn"),
    ("ca", "ca"), ("cs", "cs"), ("da", "da"), ("de", "de"),
    ("el", "el"), ("es", "es"), ("es_AR", "es_AR"), ("es_CL", "es_CL"),
    ("es_CR", "es_CR"), ("es_CU", "es_CU"), ("es_DO", "es_DO"),
    ("es_EC", "es_EC"), ("es_ES", "es_ES"), ("es_MX", "es_MX"),
    ("es_PA", "es_PA"), ("es_PR", "es_PR"),
    ("et", "et"), ("eu", "eu"), ("fa", "fa"), ("fi", "fi"),
    ("fr", "fr"), ("gl", "gl"), ("gu", "gu"),
    ("he", "he"), ("hi", "hi"), ("hr", "hr"), ("hu", "hu"),
    ("hy", "hy"), ("is", "is"), ("it", "it"),
    ("ja", "ja"), ("ka", "ka"), ("kk", "kk"), ("kn", "kn"),
    ("ko", "ko"), ("lt", "lt"), ("lv", "lv"),
    ("ml", "ml"), ("mn", "mn"), ("mr", "mr"),
    ("nl", "nl"), ("no", "no"), ("pl", "pl"),
    ("pt_BR", "pt"), ("pt_PT", "pt_PT"),
    ("ro", "ro"), ("ru", "ru"),
    ("sk", "sk"), ("sl", "sl"), ("sq", "sq"), ("sr", "sr"),
    ("sv", "sv"), ("te", "te"), ("th", "th"), ("tr", "tr"),
    ("uk", "uk"),
    ("zh_SIM", "zh"), ("zh_TRA", "zh_TRA"),
    ("zu", "zu"),
]

# Reusable opener with cookie support (thread-local for safety)
_local = threading.local()

def _get_opener():
    """Get a thread-local HTTP opener with cookie support."""
    if not hasattr(_local, "opener"):
        jar = CookieJar()
        _local.opener = build_opener(HTTPCookieProcessor(jar))
    return _local.opener

setup_shutdown()

_rate = RateLimiter(5)  # default; overwritten in main()


def load_species_with_ebird_codes() -> dict[str, str]:
    """Load species that have an eBird code.

    Prefers the built taxonomy.json, whose codes are validated against the
    eBird taxonomy (AviList can carry codes eBird has not published yet).
    Falls back to inat_data.json and the AviList CSV.

    Returns dict of scientific_name -> ebird_code.
    """
    taxonomy = load_json(TAXONOMY_JSON)
    if taxonomy:
        return {
            sci: rec["ebird_code"] for sci, rec in taxonomy.items()
            if rec.get("taxon_group") == "Aves" and rec.get("ebird_code")
        }

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
    csv_path = RAW_DIR / csv_name if csv_name else None
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
        print("ERROR: No eBird codes found. Run collectors/inat.py first or ensure AviList CSV exists.")
        raise SystemExit(1)

    return species


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
    """Normalise an eBird CDN image URL for full-resolution download.

    Input:  https://cdn.download.ams.birds.cornell.edu/api/v2/asset/46409481/900
    Output: https://cdn.download.ams.birds.cornell.edu/api/v2/asset/46409481/1800

    Replaces whatever size suffix the OG tag uses with ``/1800`` (full-res).
    Bare URLs without a size suffix also get ``/1800`` appended.
    """
    if not og_image_url:
        return ""
    # Strip any trailing /NNN size, then add /1800
    base = re.sub(r'/\d+$', '', og_image_url)
    return base + "/1800"


def _extract_asset_id(image_url: str) -> str:
    """Extract the Macaulay Library asset ID from an eBird CDN image URL.

    Input:  https://cdn.download.ams.birds.cornell.edu/api/v2/asset/244378051/900
    Output: 244378051
    """
    if not image_url:
        return ""
    m = re.search(r'/asset/(\d+)', image_url)
    return m.group(1) if m else ""


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
            "ml_asset_id": _extract_asset_id(image_url or ""),
            "image_attribution": image_alt or "",
        }

    return {"error": f"404_all_variants({ebird_code})"}


# ---------------------------------------------------------------------------
# Phase 2: eBird common names (all locales)
# ---------------------------------------------------------------------------

def ebird_taxonomy_version() -> str:
    """Return the latest eBird taxonomy version ("" if unavailable).

    Download caches are keyed by this version, so a new eBird taxonomy
    release is fetched instead of serving names cached from the old one.
    """
    req = urllib.request.Request(EBIRD_VERSIONS_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            versions = json.loads(resp.read())
    except Exception as e:
        print(f"  WARNING: eBird taxonomy version lookup failed: {e}")
        return ""
    return next((str(v.get("authorityVer")) for v in versions if v.get("latest")), "")


def _taxonomy_cache_key(prefix: str, locale: str, version: str):
    return cache_key(prefix, f"{version}:{locale}" if version else locale)


def _fetch_taxonomy_rows(locale: str) -> list[dict] | None:
    """Download eBird taxonomy CSV rows for a locale (None on failure)."""
    url = f"{EBIRD_TAXONOMY_URL}?fmt=csv&locale={locale}&cat=species"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  WARNING: eBird taxonomy download failed for {locale}: {e}")
        return None
    return [row for row in csv.DictReader(io.StringIO(data)) if row.get("SPECIES_CODE")]


def _download_ebird_taxonomy(locale: str, version: str = "") -> dict[str, str]:
    """Download eBird taxonomy CSV for a locale → {code: name}."""
    key = _taxonomy_cache_key("ebird_tax", locale, version)
    cached = cache_get(key)
    if cached is not None:
        return cached
    rows = _fetch_taxonomy_rows(locale)
    if rows is None:
        return {}
    result = {row["SPECIES_CODE"]: row["COMMON_NAME"] for row in rows}
    cache_put(key, result)
    return result


def fetch_ebird_taxonomy(version: str = "") -> dict[str, dict[str, str]]:
    """Download the English eBird taxonomy → {code: {scientific_name, common_name}}.

    The build uses it to check that a bird's eBird code exists and to fall
    back to eBird's own scientific name when AviList has no 1:1 code.
    """
    key = _taxonomy_cache_key("ebird_tax_full", "en", version)
    cached = cache_get(key)
    if cached is not None:
        return cached
    rows = _fetch_taxonomy_rows("en")
    if rows is None:
        return {}
    result = {
        row["SPECIES_CODE"]: {
            "scientific_name": (row.get("SCIENTIFIC_NAME") or "").strip(),
            "common_name": (row.get("COMMON_NAME") or "").strip(),
        }
        for row in rows
    }
    cache_put(key, result)
    return result


def fetch_ebird_names(version: str = "") -> dict[str, dict[str, str]]:
    """Download eBird taxonomy for ALL available locales.

    Returns {species_code: {canonical_locale: common_name}}.
    Only includes actual translations (skips English fallbacks).
    """
    print("    Downloading English baseline...")
    en_names = _download_ebird_taxonomy("en", version)
    if not en_names:
        print("  WARNING: Could not download eBird English taxonomy.")
        return {}

    result: dict[str, dict[str, str]] = {}
    for code, name in en_names.items():
        result.setdefault(code, {})["en"] = name

    for ebird_loc, canonical in EBIRD_LOCALES:
        print(f"    Downloading {canonical} (eBird: {ebird_loc})...",
              end=" ", flush=True)
        names = _download_ebird_taxonomy(ebird_loc, version)
        translated = 0
        for code, name in names.items():
            en_name = en_names.get(code, "")
            if name and name != en_name:
                result.setdefault(code, {})[canonical] = name
                translated += 1
        print(f"{translated}/{len(names)} translated")
        time.sleep(0.2)

    return result


def main():
    cfg = load_config()
    ebird_cfg = cfg.get("ebird", {})
    base_url = ebird_cfg.get("base_url", "https://ebird.org/species/")
    default_workers = ebird_cfg.get("workers", 4)
    default_rps = ebird_cfg.get("rps", 5)

    parser = argparse.ArgumentParser(description="Fetch eBird species descriptions, images, and common names")
    parser.add_argument("--limit", type=int, default=0, help="Max species to fetch (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without fetching")
    parser.add_argument("--workers", type=int, default=0,
                        help=f"Number of parallel fetchers (default: {default_workers})")
    parser.add_argument("--rps", type=float, default=0,
                        help=f"Max requests per second (default: {default_rps})")
    parser.add_argument("--names-only", action="store_true",
                        help="Only run Phase 2 (download common names)")
    parser.add_argument("--skip-names", action="store_true",
                        help="Skip Phase 2 (common name download)")
    args = parser.parse_args()
    if not args.workers:
        args.workers = default_workers
    if not args.rps:
        args.rps = default_rps

    # Set global rate limiter
    global _rate
    _rate = RateLimiter(args.rps)

    # Phase 1: Scrape species pages
    if not args.names_only:
        print("Phase 1: eBird species pages")
        print("Loading species with eBird codes...")
        species = load_species_with_ebird_codes()
        print(f"  Found {len(species)} species with eBird codes")

        existing = load_json(OUTPUT_FILE)
        print(f"  Already have eBird data for {len(existing)} species")

        # Pages are per code; a renamed species keeps the record fetched for its code.
        fetched_codes = {rec.get("ebird_code") for rec in existing.values() if rec.get("ebird_code")}
        to_fetch = [(sci, code) for sci, code in species.items()
                    if sci not in existing and code not in fetched_codes]
        if args.limit:
            to_fetch = to_fetch[:args.limit]

        print(f"  Will fetch {len(to_fetch)} species from eBird ({args.workers} workers, {args.rps} rps)")

        if args.dry_run:
            for sci, code in to_fetch[:20]:
                print(f"    {sci} -> {base_url}{code}")
            if len(to_fetch) > 20:
                print(f"    ... and {len(to_fetch) - 20} more")
        elif to_fetch:
            def _fetch_one(item):
                sci_name, ebird_code = item
                result = fetch_ebird_page(ebird_code, base_url)
                if result.get("error"):
                    return sci_name, {
                        "ebird_code": ebird_code,
                        "description": None,
                        "image_url": "",
                        "ml_asset_id": "",
                        "image_attribution": "",
                        "error": result["error"],
                    }
                elif result.get("description"):
                    return sci_name, {
                        "ebird_code": ebird_code,
                        "description": result["description"],
                        "image_url": result["image_url"],
                        "ml_asset_id": result.get("ml_asset_id", ""),
                        "image_attribution": result["image_attribution"],
                    }
                else:
                    return sci_name, {
                        "ebird_code": ebird_code,
                        "description": None,
                        "image_url": result.get("image_url", ""),
                        "ml_asset_id": result.get("ml_asset_id", ""),
                        "image_attribution": result.get("image_attribution", ""),
                        "error": "no_description",
                    }

            success = 0
            pbar = tqdm(total=len(to_fetch), desc="eBird", unit="sp")

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {}
                for item in to_fetch:
                    if is_shutting_down():
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
                    save_json(existing, OUTPUT_FILE)

                    if is_shutting_down():
                        for f in futures:
                            f.cancel()
                        break

            pbar.close()
            print(f"\nDone Phase 1! Fetched {pbar.n} species, {success} with descriptions.")
            print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries")

    # Phase 2: eBird common names
    if not args.skip_names and not args.dry_run and not is_shutting_down():
        version = ebird_taxonomy_version()
        print(f"\nPhase 2: eBird common names ({len(EBIRD_LOCALES)} locales, "
              f"taxonomy {version or 'unknown version'})")
        taxonomy = fetch_ebird_taxonomy(version)
        if taxonomy:
            save_json({"version": version, "species": taxonomy}, TAXONOMY_FILE)
            print(f"  Saved {len(taxonomy)} species to {TAXONOMY_FILE.name}")
        ebird_names = fetch_ebird_names(version)
        if ebird_names:
            save_json(ebird_names, NAMES_FILE)
            total_codes = len(ebird_names)
            locale_count = len(set(
                loc for names in ebird_names.values()
                for loc in names.keys()
            ))
            print(f"\n  Saved {total_codes} species × {locale_count} locales "
                  f"to {NAMES_FILE.name}")

    print("\nAll done!")


if __name__ == "__main__":
    main()
