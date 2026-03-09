#!/usr/bin/env python3
"""
Fetch Wikipedia summaries and localized article links for species.

Uses the Wikipedia REST API and MediaWiki langlinks API to collect:
- English summary text (extract)
- Localized Wikipedia article URLs for target locales

Requires inat_data.json (which provides the English Wikipedia URL).

Output: raw_data/wikipedia_data.json (incremental, resumable)

Usage:
    python -m collectors.wikipedia [--limit N] [--dry-run]

Wikipedia APIs used:
  - REST: https://en.wikipedia.org/api/rest_v1/page/summary/{title}
  - Langlinks: https://en.wikipedia.org/w/api.php?action=query&prop=langlinks
"""

import argparse
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from tqdm import tqdm

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT, setup_shutdown, is_shutting_down,
    RateLimiter, load_json, save_json,
)

INAT_DATA = RAW_DIR / "inat_data.json"
OUTPUT_FILE = RAW_DIR / "wikipedia_data.json"

setup_shutdown()

_rate = RateLimiter(50)  # default; overwritten in main()


def _wiki_request(url: str, accept: str = "application/json") -> bytes | None:
    """Make a rate-limited HTTP request with retry on transient errors."""
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
    })
    for attempt in range(4):
        _rate.acquire()
        try:
            with urlopen(req, timeout=30) as resp:
                return resp.read()
        except HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            raise
        except (URLError, TimeoutError, OSError):
            if attempt < 3:
                time.sleep(min(2 ** attempt, 10))
                continue
            return None
    return None  # exhausted retries


def load_species_with_wikipedia() -> dict[str, str]:
    """Load species that have a Wikipedia URL from iNat data.

    Returns dict of scientific_name -> wikipedia_url.
    """
    if not INAT_DATA.exists():
        print(f"ERROR: {INAT_DATA} not found. Run utils/inat.py first.")
        raise SystemExit(1)

    with open(INAT_DATA, encoding="utf-8") as f:
        inat_data = json.load(f)

    species = {}
    for sci_name, record in inat_data.items():
        if record.get("inat_id") is None:
            continue
        wiki_url = (record.get("wikipedia_url") or "").strip()
        if wiki_url:
            species[sci_name] = wiki_url

    return species


def wiki_title_from_url(url: str) -> str:
    """Extract the Wikipedia article title from a URL."""
    parsed = urlparse(url)
    # Path like /wiki/Eurasian_blue_tit
    path = parsed.path
    if "/wiki/" in path:
        return unquote(path.split("/wiki/")[-1])
    return ""


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _search_wikipedia(query: str) -> str | None:
    """Search Wikipedia for an article title matching the query.

    Useful as a fallback when the direct title lookup returns 404
    (e.g. due to article renames or taxonomic reclassifications).
    """
    url = (
        f"https://en.wikipedia.org/w/api.php?action=query&list=search"
        f"&srsearch={quote(query, safe='')}&srlimit=1&format=json"
    )
    try:
        raw = _wiki_request(url)
    except (HTTPError, URLError, TimeoutError):
        return None
    if raw is None:
        return None

    data = json.loads(raw.decode("utf-8"))
    results = data.get("query", {}).get("search", [])
    return results[0]["title"] if results else None


def _fetch_summary_for_title(title: str) -> dict | None:
    """Fetch Wikipedia summary for an exact title (no fallback)."""
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"

    try:
        raw = _wiki_request(url)
    except HTTPError:
        return None
    except (URLError, TimeoutError):
        return None
    if raw is None:
        return None

    data = json.loads(raw.decode("utf-8"))
    result = {
        "title": data.get("title", ""),
        "extract": data.get("extract", ""),
        "description": data.get("description", ""),
        "page_url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
    }

    # Extract image URL from the summary response (no extra API call)
    orig = data.get("originalimage", {})
    if orig.get("source"):
        result["image_url"] = orig["source"]

    return result


def fetch_summary(title: str, sci_name: str = "") -> dict | None:
    """Fetch Wikipedia summary via REST API (includes main image if present).

    Falls back to Wikipedia search if the direct title returns 404.
    """
    result = _fetch_summary_for_title(title)
    if result is not None:
        return result

    # Fallback: search Wikipedia by scientific name, then by original title
    for query in (sci_name, title.replace("_", " ")):
        if not query:
            continue
        found_title = _search_wikipedia(query)
        if found_title and found_title.replace(" ", "_") != title:
            result = _fetch_summary_for_title(found_title)
            if result is not None:
                return result

    return None


def fetch_langlinks(title: str, target_locales: list[str]) -> dict[str, dict]:
    """Fetch localized Wikipedia article URLs and titles via langlinks API.

    Only returns entries for languages in target_locales (excluding 'en').
    """
    target_set = {l for l in target_locales if l != "en"}
    url = (
        f"https://en.wikipedia.org/w/api.php"
        f"?action=query&titles={quote(title, safe='')}"
        f"&prop=langlinks&lllimit=500&redirects=1&format=json"
    )

    try:
        raw = _wiki_request(url)
    except (HTTPError, URLError, TimeoutError):
        return {}
    if raw is None:
        return {}

    data = json.loads(raw.decode("utf-8"))
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return {}

    page = next(iter(pages.values()))
    langlinks = page.get("langlinks", [])

    result = {}
    for ll in langlinks:
        lang = ll.get("lang", "")
        article_title = ll.get("*", "")
        if lang and article_title and lang in target_set:
            result[lang] = {
                "url": f"https://{lang}.wikipedia.org/wiki/{quote(article_title, safe='/:@!$&\'()*+,;=')}",
                "title": article_title,
            }

    return result


def fetch_image_license(image_url: str) -> dict:
    """Fetch license and attribution info from Wikimedia Commons for an image.

    Queries the Commons API for the image's extmetadata (artist, license).
    Returns dict with artist, license_short, license_url (all may be empty).
    """
    filename = unquote(image_url.split("/")[-1])
    if not filename:
        return {}

    api_url = (
        f"https://commons.wikimedia.org/w/api.php?action=query"
        f"&titles=File:{quote(filename, safe='')}"
        f"&prop=imageinfo&iiprop=extmetadata&format=json"
    )
    try:
        raw = _wiki_request(api_url)
    except (HTTPError, URLError, TimeoutError):
        return {}
    if raw is None:
        return {}

    data = json.loads(raw.decode("utf-8"))
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return {}

    page = next(iter(pages.values()))
    meta = page.get("imageinfo", [{}])[0].get("extmetadata", {})

    artist_html = meta.get("Artist", {}).get("value", "")
    return {
        "artist": _strip_html(artist_html),
        "license_short": meta.get("LicenseShortName", {}).get("value", ""),
        "license_url": meta.get("LicenseUrl", {}).get("value", ""),
    }


def fetch_locale_extract(lang: str, title: str) -> str | None:
    """Fetch Wikipedia summary extract for a specific language edition."""
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"

    try:
        raw = _wiki_request(url)
    except (HTTPError, URLError, TimeoutError):
        return None
    if raw is None:
        return None

    data = json.loads(raw.decode("utf-8"))
    return data.get("extract", "") or None


def fetch_species_wikipedia(sci_name: str, wiki_url: str,
                            target_locales: list[str]) -> tuple[str, dict]:
    """Fetch Wikipedia data for one species.

    Fetches the English summary, langlinks filtered to target_locales,
    and locale extracts (summaries) for each matched language.

    Returns (sci_name, record_dict).
    """
    title = wiki_title_from_url(wiki_url)
    if not title:
        return sci_name, {"error": "bad_url", "wikipedia_url": wiki_url}

    # Fetch English summary
    summary = fetch_summary(title, sci_name=sci_name)
    resolved_title = summary["title"] if summary else title

    # Fetch langlinks filtered to configured locales
    locale_links = fetch_langlinks(resolved_title, target_locales)
    locale_urls = {lang: info["url"] for lang, info in locale_links.items()}

    # Fetch locale extracts in parallel
    locale_extracts: dict[str, str] = {}

    def _fetch_one(lang_info: tuple[str, dict]) -> tuple[str, str | None]:
        lang, info = lang_info
        return lang, fetch_locale_extract(lang, info["title"])

    if locale_links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for lang, ext in pool.map(_fetch_one, locale_links.items()):
                if ext:
                    locale_extracts[lang] = ext

    if summary:
        record = {
            "title": summary["title"],
            "extract": summary["extract"],
            "description": summary["description"],
            "wikipedia_urls": {"en": summary["page_url"] or wiki_url},
            "extracts": {"en": summary["extract"]} if summary["extract"] else {},
        }
        record["wikipedia_urls"].update(locale_urls)
        record["extracts"].update(locale_extracts)

        # Fetch image + license from Wikimedia Commons
        img_url = summary.get("image_url", "")
        if img_url:
            record["image_url"] = img_url
            license_info = fetch_image_license(img_url)
            if license_info:
                record["image_artist"] = license_info.get("artist", "")
                record["image_license"] = license_info.get("license_short", "")
                record["image_license_url"] = license_info.get("license_url", "")
    else:
        record = {
            "error": "not_found",
            "wikipedia_url": wiki_url,
            "wikipedia_urls": {},
            "extracts": {},
        }
        if locale_urls:
            record["wikipedia_urls"] = locale_urls
        if locale_extracts:
            record["extracts"] = locale_extracts

    return sci_name, record


def main():
    cfg = load_config()
    wiki_cfg = cfg.get("wikipedia", {})
    default_workers = wiki_cfg.get("workers", 4)
    default_rps = wiki_cfg.get("rps", 50)

    parser = argparse.ArgumentParser(description="Fetch Wikipedia data for species")
    parser.add_argument("--limit", type=int, default=0, help="Max species to fetch (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without fetching")
    parser.add_argument("--refetch", action="store_true",
                        help="Re-fetch species that have few locale extracts")
    parser.add_argument("--workers", type=int, default=default_workers,
                        help=f"Number of parallel species to fetch (default: {default_workers})")
    parser.add_argument("--rps", type=float, default=default_rps,
                        help=f"Max requests per second across all threads (default: {default_rps})")
    args = parser.parse_args()

    # Set global rate limiter
    global _rate
    _rate = RateLimiter(args.rps)

    target_locales = wiki_cfg.get("locales", ["en"])

    print("Loading species with Wikipedia URLs...")
    species = load_species_with_wikipedia()
    print(f"  Found {len(species)} species with Wikipedia URLs")

    existing = load_json(OUTPUT_FILE)
    print(f"  Already have Wikipedia data for {len(existing)} species")

    if args.refetch:
        # Re-fetch species that have fewer than 2 locale extracts
        # (i.e. only English or empty — likely fetched before locale support)
        to_fetch = [
            (sci, species[sci])
            for sci in existing
            if sci in species and len(existing[sci].get("extracts", {})) < 2
        ]
    else:
        # Fetch species not yet processed, plus any that errored previously
        to_fetch = [
            (sci, url) for sci, url in species.items()
            if sci not in existing or existing[sci].get("error")
        ]
    if args.limit:
        to_fetch = to_fetch[:args.limit]

    print(f"  Will fetch {len(to_fetch)} species from Wikipedia ({args.workers} workers)")
    print(f"  Target locales: {', '.join(target_locales)}")

    if args.dry_run:
        for sci, url in to_fetch[:20]:
            print(f"    {sci} -> {url}")
        if len(to_fetch) > 20:
            print(f"    ... and {len(to_fetch) - 20} more")
        return

    success = 0
    pbar = tqdm(total=len(to_fetch), desc="Wikipedia", unit="sp")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for sci, url in to_fetch:
            if is_shutting_down():
                break
            futures[pool.submit(fetch_species_wikipedia, sci, url, target_locales)] = sci

        for future in as_completed(futures):
            sci_name = futures[future]
            try:
                _, record = future.result()
                existing[sci_name] = record
                if "error" not in record:
                    success += 1
                    n_ext = len(record.get("extracts", {}))
                    pbar.set_postfix_str(f"{sci_name} ({n_ext} loc)", refresh=False)
                else:
                    tqdm.write(f"  {record.get('error', '').upper()} {sci_name}")
            except Exception as exc:
                tqdm.write(f"  EXCEPTION {sci_name}: {exc}")
                existing[sci_name] = {"error": str(exc)}
            pbar.update(1)
            save_json(existing, OUTPUT_FILE)

            if is_shutting_down():
                # Cancel remaining futures
                for f in futures:
                    f.cancel()
                break

    pbar.close()
    print(f"\nDone! Fetched {len(to_fetch)} species, {success} with summaries.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries")


if __name__ == "__main__":
    main()