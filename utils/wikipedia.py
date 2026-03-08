#!/usr/bin/env python3
"""
Fetch Wikipedia summaries and localized article links for species.

Uses the Wikipedia REST API and MediaWiki langlinks API to collect:
- English summary text (extract)
- Localized Wikipedia article URLs for target locales

Requires inat_data.json (which provides the English Wikipedia URL).

Output: raw_data/wikipedia_data.json (incremental, resumable)

Usage:
    python -m utils.wikipedia [--limit N] [--dry-run]

Wikipedia APIs used:
  - REST: https://en.wikipedia.org/api/rest_v1/page/summary/{title}
  - Langlinks: https://en.wikipedia.org/w/api.php?action=query&prop=langlinks
"""

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Paths
ROOT = Path(__file__).resolve().parent.parent
INAT_DATA = ROOT / "raw_data" / "inat_data.json"
OUTPUT_FILE = ROOT / "raw_data" / "wikipedia_data.json"

from tqdm import tqdm

from utils.config import load_config

USER_AGENT = "species-data-collector/1.0 (https://github.com/birdnet-team/species-data)"

# ---------------------------------------------------------------------------
# Rate limiter — caps requests/second across all threads
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

_rate = RateLimiter(50)  # default; overwritten in main()


def _wiki_request(url: str, accept: str = "application/json") -> bytes | None:
    """Make a rate-limited HTTP request with retry on 429."""
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
            if e.code == 429:
                wait = min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            raise
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


def load_existing_data() -> dict:
    """Load already-fetched Wikipedia data."""
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    """Save Wikipedia data to disk."""
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def wiki_title_from_url(url: str) -> str:
    """Extract the Wikipedia article title from a URL."""
    parsed = urlparse(url)
    # Path like /wiki/Eurasian_blue_tit
    path = parsed.path
    if "/wiki/" in path:
        return unquote(path.split("/wiki/")[-1])
    return ""


def fetch_summary(title: str) -> dict | None:
    """Fetch Wikipedia summary via REST API."""
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"

    try:
        raw = _wiki_request(url)
    except HTTPError as e:
        if e.code == 404:
            return None
        return None
    except (URLError, TimeoutError):
        return None
    if raw is None:
        return None

    data = json.loads(raw.decode("utf-8"))
    return {
        "title": data.get("title", ""),
        "extract": data.get("extract", ""),
        "description": data.get("description", ""),
        "page_url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
    }


def fetch_langlinks(title: str, target_locales: list[str]) -> dict[str, dict]:
    """Fetch localized Wikipedia article URLs and titles via langlinks API.

    Returns dict of lang -> {"url": ..., "title": ...} for target locales.
    """
    target_set = set(l for l in target_locales if l != "en")
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
    """Fetch all Wikipedia data for one species (summary + langlinks + locale extracts).

    Returns (sci_name, record_dict).
    """
    title = wiki_title_from_url(wiki_url)
    if not title:
        return sci_name, {"error": "bad_url", "wikipedia_url": wiki_url}

    # Fetch English summary
    summary = fetch_summary(title)
    resolved_title = summary["title"] if summary else title

    # Fetch langlinks
    locale_links = fetch_langlinks(resolved_title, target_locales)

    # Fetch all locale extracts in parallel
    locale_urls = {lang: info["url"] for lang, info in locale_links.items()}
    locale_extracts = {}

    def _fetch_one(lang_info):
        lang, info = lang_info
        ext = fetch_locale_extract(lang, info["title"])
        return lang, ext

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
    parser = argparse.ArgumentParser(description="Fetch Wikipedia data for species")
    parser.add_argument("--limit", type=int, default=0, help="Max species to fetch (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without fetching")
    parser.add_argument("--refetch", action="store_true",
                        help="Re-fetch species that are missing localized extracts")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel species to fetch (default: 4)")
    parser.add_argument("--rps", type=float, default=50,
                        help="Max requests per second across all threads (default: 50)")
    args = parser.parse_args()

    # Set global rate limiter
    global _rate
    _rate = RateLimiter(args.rps)

    cfg = load_config()
    target_locales = cfg.get("locales", ["en"])

    print("Loading species with Wikipedia URLs...")
    species = load_species_with_wikipedia()
    print(f"  Found {len(species)} species with Wikipedia URLs")

    existing = load_existing_data()
    print(f"  Already have Wikipedia data for {len(existing)} species")

    if args.refetch:
        to_fetch = [
            (sci, species[sci])
            for sci in existing
            if sci in species and not existing[sci].get("extracts")
        ]
    else:
        to_fetch = [(sci, url) for sci, url in species.items() if sci not in existing]
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
        futures = {
            pool.submit(fetch_species_wikipedia, sci, url, target_locales): sci
            for sci, url in to_fetch
        }
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
            save_data(existing)

    pbar.close()
    print(f"\nDone! Fetched {len(to_fetch)} species, {success} with summaries.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries")


if __name__ == "__main__":
    main()