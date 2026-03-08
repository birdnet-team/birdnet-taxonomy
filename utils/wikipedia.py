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
import time
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
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })

    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 404:
            return None
        print(f"  ERROR fetching summary: {e}")
        return None
    except (URLError, TimeoutError) as e:
        print(f"  ERROR fetching summary: {e}")
        return None

    return {
        "title": data.get("title", ""),
        "extract": data.get("extract", ""),
        "description": data.get("description", ""),
        "page_url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
    }


def fetch_langlinks(title: str, target_locales: list[str]) -> dict[str, str]:
    """Fetch localized Wikipedia article URLs via langlinks API."""
    target_set = set(l for l in target_locales if l != "en")
    url = (
        f"https://en.wikipedia.org/w/api.php"
        f"?action=query&titles={quote(title, safe='')}"
        f"&prop=langlinks&lllimit=500&redirects=1&format=json"
    )
    req = Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"  ERROR fetching langlinks: {e}")
        return {}

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
            result[lang] = f"https://{lang}.wikipedia.org/wiki/{quote(article_title, safe='/:@!$&\'()*+,;=')}"

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch Wikipedia data for species")
    parser.add_argument("--limit", type=int, default=0, help="Max species to fetch (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without fetching")
    args = parser.parse_args()

    cfg = load_config()
    target_locales = cfg.get("locales", ["en"])
    wiki_cfg = cfg.get("wikipedia", {})
    delay = wiki_cfg.get("request_delay", 0.5)

    print("Loading species with Wikipedia URLs...")
    species = load_species_with_wikipedia()
    print(f"  Found {len(species)} species with Wikipedia URLs")

    existing = load_existing_data()
    print(f"  Already have Wikipedia data for {len(existing)} species")

    to_fetch = [(sci, url) for sci, url in species.items() if sci not in existing]
    if args.limit:
        to_fetch = to_fetch[:args.limit]

    print(f"  Will fetch {len(to_fetch)} species from Wikipedia")
    print(f"  Target locales: {', '.join(target_locales)}")

    if args.dry_run:
        for sci, url in to_fetch[:20]:
            print(f"    {sci} -> {url}")
        if len(to_fetch) > 20:
            print(f"    ... and {len(to_fetch) - 20} more")
        return

    fetched = 0
    success = 0
    pbar = tqdm(to_fetch, desc="Wikipedia", unit="sp")
    for sci_name, wiki_url in pbar:
        title = wiki_title_from_url(wiki_url)
        if not title:
            tqdm.write(f"  BAD URL {sci_name}: {wiki_url}")
            existing[sci_name] = {"error": "bad_url", "wikipedia_url": wiki_url}
            fetched += 1
            continue

        pbar.set_postfix_str(sci_name, refresh=False)

        # Fetch summary
        summary = fetch_summary(title)
        time.sleep(delay)

        # Use resolved title from summary (follows redirects) for langlinks
        resolved_title = summary["title"] if summary else title

        # Fetch langlinks
        locale_urls = fetch_langlinks(resolved_title, target_locales)
        time.sleep(delay)

        if summary:
            record = {
                "title": summary["title"],
                "extract": summary["extract"],
                "description": summary["description"],
                "wikipedia_urls": {"en": summary["page_url"] or wiki_url},
            }
            record["wikipedia_urls"].update(locale_urls)
            existing[sci_name] = record
            success += 1
        else:
            existing[sci_name] = {
                "error": "not_found",
                "wikipedia_url": wiki_url,
                "wikipedia_urls": {},
            }
            # Even without summary, we might have langlinks
            if locale_urls:
                existing[sci_name]["wikipedia_urls"] = locale_urls
            tqdm.write(f"  NO SUMMARY {sci_name}")

        fetched += 1
        save_data(existing)

    pbar.close()
    print(f"\nDone! Fetched {fetched} species, {success} with summaries.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries")


if __name__ == "__main__":
    main()