#!/usr/bin/env python3
"""
Fetch Wikipedia summaries and localized article links for species.

Uses the MediaWiki action=query API with batching (up to 50 titles per
request) to minimize the number of HTTP requests.

Three phases:
  Phase 1 — English Wikipedia: extracts, langlinks, page images, descriptions
  Phase 2 — Locale extracts: for each target language, batch-fetch summaries
  Phase 3 — Image licenses: batch-fetch from Wikimedia Commons

Requires inat_data.json (which provides the English Wikipedia URL).

Output: raw_data/wikipedia_data.json (incremental, resumable)

Usage:
    python -m collectors.wikipedia [--limit N] [--dry-run] [--refetch]

Wikipedia APIs used:
  - action=query with prop=extracts|langlinks|pageimages|pageterms
  - action=query with prop=extracts  (on locale wikis)
  - action=query with prop=imageinfo  (on Commons)
"""

import argparse
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from tqdm import tqdm

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT, setup_shutdown, is_shutting_down,
    RateLimiter, is_full_species_name, load_json, save_json,
)

INAT_DATA = RAW_DIR / "inat_data.json"
OUTPUT_FILE = RAW_DIR / "wikipedia_data.json"

EN_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
BATCH_SIZE = 50  # max titles per MediaWiki query

setup_shutdown()

_rate = RateLimiter(25)  # default; overwritten in main()


class _FetchFailed(Exception):
    """Raised when all retry attempts for a Wikipedia request are exhausted."""
    pass


# Global cooldown: when any thread gets a 429, all threads pause.
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0  # monotonic timestamp


def _apply_cooldown(seconds: float):
    """Set a global cooldown after hitting a 429."""
    global _cooldown_until
    target = time.monotonic() + seconds
    with _cooldown_lock:
        if target > _cooldown_until:
            _cooldown_until = target


def _wait_cooldown():
    """Block until any active cooldown expires."""
    with _cooldown_lock:
        remaining = _cooldown_until - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _wiki_request(url: str) -> bytes:
    """Make a rate-limited HTTP request with retry on transient errors.

    Returns response bytes on success.
    Raises HTTPError for non-retryable HTTP errors (e.g. 404).
    Raises _FetchFailed after exhausting retries on transient errors.
    """
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    last_err = None
    for attempt in range(6):
        _wait_cooldown()
        _rate.acquire()
        try:
            with urlopen(req, timeout=30) as resp:
                return resp.read()
        except HTTPError as e:
            if e.code == 429:
                try:
                    wait = max(float(e.headers.get("Retry-After", "")), 2.0)
                except (ValueError, TypeError):
                    wait = min(2 ** (attempt + 1), 30)
                _apply_cooldown(wait)
                last_err = e
                time.sleep(wait)
                continue
            if e.code >= 500:
                last_err = e
                time.sleep(min(2 ** attempt, 30))
                continue
            raise  # 404, 403, etc. — non-retryable
        except (URLError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))
            continue
    raise _FetchFailed(f"After 6 retries: {last_err}")


# ── Helpers ───────────────────────────────────────────────────────────

def load_species_with_wikipedia() -> dict[str, str]:
    """Load species that have a Wikipedia URL from iNat data.

    Returns dict of scientific_name -> wikipedia_url.
    """
    if not INAT_DATA.exists():
        print(f"ERROR: {INAT_DATA} not found. Run collectors/inat.py first.")
        raise SystemExit(1)

    with open(INAT_DATA, encoding="utf-8") as f:
        inat_data = json.load(f)

    species = {}
    for sci_name, record in inat_data.items():
        if record.get("inat_id") is None:
            continue
        if not is_full_species_name(sci_name):
            continue
        wiki_url = (record.get("wikipedia_url") or "").strip()
        if wiki_url:
            species[sci_name] = wiki_url

    return species


def wiki_title_from_url(url: str) -> str:
    """Extract the Wikipedia article title from a URL."""
    parsed = urlparse(url)
    path = parsed.path
    if "/wiki/" in path:
        return unquote(path.split("/wiki/")[-1])
    return ""


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _search_wikipedia(query: str) -> str | None:
    """Search Wikipedia for an article title matching the query."""
    url = (
        f"{EN_API}?action=query&list=search"
        f"&srsearch={quote(query, safe='')}&srlimit=1&format=json"
    )
    try:
        raw = _wiki_request(url)
    except (HTTPError, _FetchFailed):
        return None

    data = json.loads(raw.decode("utf-8"))
    results = data.get("query", {}).get("search", [])
    return results[0]["title"] if results else None


def _chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ── Batch query engine ────────────────────────────────────────────────

def _batch_query(api_url: str, titles: list[str], props: str,
                 extra_params: dict | None = None) -> dict[str, dict]:
    """Query a MediaWiki API for multiple titles at once.

    Handles continuation (e.g. for large langlinks results) and
    title normalization / redirects.

    Returns dict of input_title -> page_data.
    """
    params = {
        "action": "query",
        "titles": "|".join(titles),
        "prop": props,
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
    }
    if extra_params:
        params.update(extra_params)

    pages_by_id: dict[int, dict] = {}
    title_chain: dict[str, str] = {}  # resolved_title -> input_title

    while True:
        if is_shutting_down():
            break

        url = f"{api_url}?" + urlencode(params, doseq=True)
        try:
            raw = _wiki_request(url)
        except _FetchFailed:
            break
        data = json.loads(raw.decode("utf-8"))
        query = data.get("query", {})

        # Build title resolution chain (first request only)
        if not title_chain:
            resolved = {t: t for t in titles}
            for norm in query.get("normalized", []):
                old, new = norm.get("from", ""), norm.get("to", "")
                for inp in list(resolved):
                    if resolved[inp] == old:
                        resolved[inp] = new
            for redir in query.get("redirects", []):
                old, new = redir.get("from", ""), redir.get("to", "")
                for inp in list(resolved):
                    if resolved[inp] == old:
                        resolved[inp] = new
            # Invert: page_title -> input_title
            title_chain = {v: k for k, v in resolved.items()}

        # Merge pages
        for page in query.get("pages", []):
            pid = page.get("pageid")
            if pid is None or page.get("missing"):
                continue
            if pid in pages_by_id:
                # Merge list-type properties from continuation
                for key in ("langlinks",):
                    old_list = pages_by_id[pid].get(key, [])
                    new_list = page.get(key, [])
                    if new_list:
                        pages_by_id[pid][key] = old_list + new_list
            else:
                pages_by_id[pid] = page

        if "continue" not in data:
            break
        params.update(data["continue"])

    # Map back to input titles
    result = {}
    for page in pages_by_id.values():
        page_title = page.get("title", "")
        input_title = title_chain.get(page_title, page_title)
        result[input_title] = page

    return result


# ── Phase 1: English Wikipedia ────────────────────────────────────────

def _batch_fetch_english(titles: list[str],
                         target_locales: list[str]) -> dict[str, dict]:
    """Batch fetch extracts + langlinks + images + descriptions
    for up to 50 titles from English Wikipedia.

    Returns dict of input_title -> {
        title, extract, description, image_url,
        langlinks: {lang: {url, title}},
    }
    """
    target_set = {l for l in target_locales if l != "en"}

    pages = _batch_query(EN_API, titles,
        props="extracts|langlinks|pageimages|pageterms",
        extra_params={
            "exintro": "1", "explaintext": "1", "exlimit": str(BATCH_SIZE),
            "lllimit": "500",
            "piprop": "original", "pilimit": str(BATCH_SIZE),
            "wbptterms": "description",
        },
    )

    result = {}
    for input_title, page in pages.items():
        # Filter langlinks to target locales
        filtered_ll = {}
        for ll in page.get("langlinks", []):
            lang = ll.get("lang", "")
            loc_title = ll.get("title", "")
            if lang in target_set and loc_title:
                safe_title = quote(loc_title, safe="/:@!$&'()*+,;=")
                filtered_ll[lang] = {
                    "url": f"https://{lang}.wikipedia.org/wiki/{safe_title}",
                    "title": loc_title,
                }

        # Image from pageimages
        image_url = (page.get("original") or {}).get("source", "")

        # Description from pageterms
        terms = page.get("terms") or {}
        descriptions = terms.get("description") or []
        description = descriptions[0] if descriptions else ""

        result[input_title] = {
            "title": page.get("title", ""),
            "extract": page.get("extract", ""),
            "description": description,
            "image_url": image_url,
            "langlinks": filtered_ll,
        }

    return result


# ── Phase 2: Locale extracts ─────────────────────────────────────────

def _batch_fetch_locale_extracts(lang: str,
                                 titles: list[str]) -> dict[str, str]:
    """Batch fetch intro extracts from {lang}.wikipedia.org.

    Returns dict of input_title -> extract_text.
    """
    api = f"https://{lang}.wikipedia.org/w/api.php"
    pages = _batch_query(api, titles,
        props="extracts",
        extra_params={
            "exintro": "1", "explaintext": "1",
            "exlimit": str(BATCH_SIZE),
        },
    )

    return {
        title: page.get("extract", "")
        for title, page in pages.items()
        if page.get("extract")
    }


# ── Phase 3: Image licenses ──────────────────────────────────────────

def _batch_fetch_licenses(filenames: list[str]) -> dict[str, dict]:
    """Batch fetch image license info from Wikimedia Commons.

    Returns dict of filename -> {artist, license_short, license_url}.
    """
    file_titles = [f"File:{fn}" for fn in filenames]

    params = {
        "action": "query",
        "titles": "|".join(file_titles),
        "prop": "imageinfo",
        "iiprop": "extmetadata",
        "format": "json",
        "formatversion": "2",
    }
    url = f"{COMMONS_API}?" + urlencode(params, doseq=True)

    try:
        raw = _wiki_request(url)
    except (HTTPError, _FetchFailed):
        return {}

    data = json.loads(raw.decode("utf-8"))
    result = {}

    for page in data.get("query", {}).get("pages", []):
        title = page.get("title", "")
        if not title.startswith("File:") or page.get("missing"):
            continue
        filename = title[5:]  # Remove "File:" prefix

        meta = (page.get("imageinfo") or [{}])[0].get("extmetadata") or {}
        artist_html = (meta.get("Artist") or {}).get("value", "")
        result[filename] = {
            "artist": _strip_html(artist_html),
            "license_short": (meta.get("LicenseShortName") or {}).get("value", ""),
            "license_url": (meta.get("LicenseUrl") or {}).get("value", ""),
        }

    return result


# ── Phase runners ─────────────────────────────────────────────────────

def _run_phase1(titles: list[str], title_to_sci: dict[str, str],
                target_locales: list[str],
                pbar: tqdm) -> dict[str, dict]:
    """Phase 1: Batch fetch English Wikipedia data for all titles."""
    english_data = {}

    for batch in _chunks(titles, BATCH_SIZE):
        if is_shutting_down():
            break
        result = _batch_fetch_english(batch, target_locales)
        english_data.update(result)
        pbar.update(len(batch))

    # Search fallback for titles not found in batch results
    missing = [t for t in titles if t not in english_data]
    if missing and not is_shutting_down():
        pbar.set_description("Fallbacks")
        for title in missing:
            if is_shutting_down():
                break
            sci = title_to_sci.get(title, "")
            for query in (sci, title.replace("_", " ")):
                if not query:
                    continue
                found = _search_wikipedia(query)
                if found and found != title.replace("_", " "):
                    result = _batch_fetch_english([found], target_locales)
                    if result:
                        english_data[title] = next(iter(result.values()))
                        break
            pbar.update(1)

    return english_data


def _run_extract_backfill(existing: dict, pbar: tqdm) -> int:
    """Backfill missing English extracts for species that have an en URL."""
    to_backfill: list[tuple[str, str]] = []  # (sci_name, title)
    for sci, rec in existing.items():
        if rec.get("extract"):
            continue
        en_url = rec.get("wikipedia_urls", {}).get("en", "")
        if not en_url:
            continue
        title = wiki_title_from_url(en_url)
        if title:
            to_backfill.append((sci, title.replace("_", " ")))

    if not to_backfill:
        pbar.close()
        return 0

    pbar.reset(total=len(to_backfill))
    updated = 0

    for batch in _chunks(to_backfill, BATCH_SIZE):
        if is_shutting_down():
            break
        titles = [t for _, t in batch]
        pages = _batch_query(EN_API, titles,
            props="extracts",
            extra_params={
                "exintro": "1", "explaintext": "1",
                "exlimit": str(BATCH_SIZE),
            },
        )
        for sci, title in batch:
            page = pages.get(title)
            if page and page.get("extract"):
                existing[sci]["extract"] = page["extract"]
                existing[sci].setdefault("extracts", {})["en"] = page["extract"]
                updated += 1
        pbar.update(len(batch))

    pbar.close()
    return updated


def _run_phase2(english_data: dict[str, dict],
                existing_extracts: dict[str, dict[str, str]],
                pbar: tqdm) -> dict[str, dict[str, str]]:
    """Phase 2: Batch fetch locale extracts for all languages.

    Skips (input_title, lang) pairs that already have an extract in
    existing_extracts.  Fetches all locales concurrently using threads.
    """
    # Collect per-locale work: lang -> [(locale_title, input_title)]
    locale_work: dict[str, list[tuple[str, str]]] = {}
    skipped = 0
    for input_title, data in english_data.items():
        already = existing_extracts.get(input_title, {})
        for lang, info in data.get("langlinks", {}).items():
            if lang in already:
                skipped += 1
                continue
            locale_work.setdefault(lang, []).append(
                (info["title"], input_title)
            )

    total_new = sum(len(v) for v in locale_work.values())
    if skipped:
        pbar.total = total_new
        pbar.refresh()

    locale_extracts: dict[str, dict[str, str]] = {}
    _lock = threading.Lock()

    def _fetch_lang(lang: str, pairs: list[tuple[str, str]]):
        """Fetch all batches for a single locale."""
        loc_titles = [p[0] for p in pairs]
        loc_to_input = {p[0]: p[1] for p in pairs}
        results: dict[str, dict[str, str]] = {}

        for batch in _chunks(loc_titles, BATCH_SIZE):
            if is_shutting_down():
                break
            extracts = _batch_fetch_locale_extracts(lang, batch)
            for loc_title, ext in extracts.items():
                input_title = loc_to_input.get(loc_title, "")
                if input_title:
                    results.setdefault(input_title, {})[lang] = ext
            with _lock:
                pbar.update(len(batch))

        return results

    # Run all locales concurrently (one thread per locale)
    n_workers = min(len(locale_work), 8)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_fetch_lang, lang, pairs): lang
            for lang, pairs in sorted(locale_work.items())
        }
        for future in as_completed(futures):
            if is_shutting_down():
                break
            results = future.result()
            for input_title, lang_texts in results.items():
                locale_extracts.setdefault(input_title, {}).update(lang_texts)

    return locale_extracts


def _run_phase3(english_data: dict[str, dict],
                existing_licenses: set[str],
                pbar: tqdm) -> dict[str, dict]:
    """Phase 3: Batch fetch image licenses from Wikimedia Commons.

    Skips species that already have license info (in existing_licenses set
    of input_titles).
    """
    images: dict[str, str] = {}  # filename -> input_title
    skipped = 0
    for input_title, data in english_data.items():
        if input_title in existing_licenses:
            skipped += 1
            continue
        img = data.get("image_url", "")
        if img:
            filename = unquote(img.split("/")[-1])
            if filename:
                images[filename] = input_title

    if skipped:
        pbar.total = len(images)
        pbar.refresh()

    title_licenses: dict[str, dict] = {}
    filenames = list(images.keys())

    for batch in _chunks(filenames, BATCH_SIZE):
        if is_shutting_down():
            break
        lics = _batch_fetch_licenses(batch)
        for fn, info in lics.items():
            input_title = images.get(fn, "")
            if input_title:
                title_licenses[input_title] = info
        pbar.update(len(batch))

    return title_licenses


def _assemble_records(
    work: list[tuple[str, str]],
    english_data: dict[str, dict],
    locale_extracts: dict[str, dict[str, str]],
    title_licenses: dict[str, dict],
    existing: dict,
):
    """Assemble final Wikipedia records from the three phases.

    Merges with existing records — preserves previously-fetched locale
    extracts and image licenses not in the current run.
    """
    for sci_name, wiki_title in work:
        data = english_data.get(wiki_title)
        prev = existing.get(sci_name, {})

        if data:
            page_title = data.get("title", wiki_title)
            safe = quote(page_title, safe="/:@!$&'()*+,;=")
            extract = data.get("extract") or prev.get("extract", "")
            description = data.get("description") or prev.get("description", "")
            wikipedia_urls = dict(prev.get("wikipedia_urls", {}))
            wikipedia_urls["en"] = f"https://en.wikipedia.org/wiki/{safe}"
            record = {
                "title": page_title,
                "extract": extract,
                "description": description,
                "wikipedia_urls": wikipedia_urls,
                "extracts": {},
            }
            if extract:
                record["extracts"]["en"] = extract

            # Locale URLs and extracts
            for lang, info in data.get("langlinks", {}).items():
                record["wikipedia_urls"][lang] = info["url"]

            # Start with existing extracts, overlay new ones
            merged_extracts = dict(prev.get("extracts", {}))
            merged_extracts.update(record["extracts"])
            if wiki_title in locale_extracts:
                merged_extracts.update(locale_extracts[wiki_title])
            record["extracts"] = merged_extracts

            # Image + license (prefer new data, fall back to existing)
            img_url = data.get("image_url", "") or prev.get("image_url", "")
            if img_url:
                record["image_url"] = img_url
                lic = title_licenses.get(wiki_title, {})
                if lic:
                    record["image_artist"] = lic.get("artist", "")
                    record["image_license"] = lic.get("license_short", "")
                    record["image_license_url"] = lic.get("license_url", "")
                elif prev.get("image_license"):
                    record["image_artist"] = prev.get("image_artist", "")
                    record["image_license"] = prev.get("image_license", "")
                    record["image_license_url"] = prev.get("image_license_url", "")
        else:
            wiki_url = f"http://en.wikipedia.org/wiki/{quote(wiki_title, safe='')}"
            record = {
                "error": "not_found",
                "wikipedia_url": wiki_url,
                "wikipedia_urls": {},
                "extracts": {},
            }

        existing[sci_name] = record


def _collect_incomplete_work(
    species: dict[str, str],
    existing: dict,
    queued_species: set[str],
) -> list[tuple[str, str]]:
    """Find species that already have Phase 1 data but still need completion."""
    incomplete: list[tuple[str, str]] = []
    for sci, url in species.items():
        if sci in queued_species:
            continue
        rec = existing.get(sci)
        if not rec or not rec.get("extract"):
            continue
        title = wiki_title_from_url(url)
        if not title:
            continue
        needs_locales = len(rec.get("extracts", {})) < len(
            rec.get("wikipedia_urls", {})
        )
        needs_license = (
            rec.get("image_url") and not rec.get("image_license")
        )
        if needs_locales or needs_license:
            incomplete.append((sci, title))
    return incomplete


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    wiki_cfg = cfg.get("wikipedia", {})
    default_rps = wiki_cfg.get("rps", 25)

    parser = argparse.ArgumentParser(
        description="Fetch Wikipedia data for species (batched)"
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Max species to fetch (0 = all)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be fetched without fetching",
    )
    parser.add_argument(
        "--refetch", action="store_true",
        help="Re-fetch species that have few locale extracts",
    )
    parser.add_argument(
        "--rps", type=float, default=default_rps,
        help=f"Max requests per second (default: {default_rps})",
    )
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

    # Build work list: separate new species from incomplete ones
    if args.refetch:
        to_fetch = [
            (sci, species[sci])
            for sci in existing
            if sci in species and len(existing[sci].get("extracts", {})) < 2
        ]
    else:
        to_fetch = [
            (sci, url) for sci, url in species.items()
            if sci not in existing or existing[sci].get("error")
        ]
    if args.limit:
        to_fetch = to_fetch[:args.limit]

    # Build (sci_name, wiki_title) pairs for new species
    work = []
    for sci, url in to_fetch:
        title = wiki_title_from_url(url)
        if title:
            work.append((sci, title))

    # Find incomplete species that need Phase 2 (locale extracts) or
    # Phase 3 (image licenses) but already have Phase 1 data
    queued_species = {s for s, _ in work}
    incomplete = _collect_incomplete_work(species, existing, queued_species)

    if args.limit:
        remaining = max(0, args.limit - len(work))
        incomplete = incomplete[:remaining]

    print(f"  Will fetch {len(work)} new species")
    print(f"  Will complete {len(incomplete)} incomplete species "
          f"(missing locales/licenses)")
    print(f"  Target locales: {', '.join(target_locales)}")

    if args.dry_run:
        for sci, title in work[:20]:
            print(f"    {sci} -> {title}")
        if len(work) > 20:
            print(f"    ... and {len(work) - 20} more")
        if incomplete:
            print(f"  Incomplete (first 10):")
            for sci, title in incomplete[:10]:
                rec = existing.get(sci, {})
                n_ext = len(rec.get("extracts", {}))
                n_url = len(rec.get("wikipedia_urls", {}))
                has_lic = "yes" if rec.get("image_license") else "no"
                print(f"    {sci}: {n_ext}/{n_url} extracts, license={has_lic}")

        total_work = len(work) + len(incomplete)
        n_locales = len([l for l in target_locales if l != "en"])
        en_batches = (len(work) + BATCH_SIZE - 1) // BATCH_SIZE
        est_locale_batches = int(n_locales * total_work * 0.6 / BATCH_SIZE) + n_locales
        est_license_batches = (total_work + BATCH_SIZE - 1) // BATCH_SIZE
        total_est = en_batches * 2 + est_locale_batches + est_license_batches
        print(f"\n  Estimated requests: ~{total_est}"
              f" (Phase 1: ~{en_batches * 2},"
              f" Phase 2: ~{est_locale_batches},"
              f" Phase 3: ~{est_license_batches})")
        print(f"  Estimated time at {args.rps} rps: ~{total_est / args.rps / 60:.0f} min")
        return

    title_to_sci = {title: sci for sci, title in work}
    all_titles = [title for _, title in work]

    # ── Phase 1: English Wikipedia (new species only) ─────────────────
    english_data = {}
    if all_titles:
        en_batches = (len(all_titles) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\nPhase 1: English Wikipedia ({en_batches} batches, "
              f"{len(all_titles)} new species)...")
        pbar1 = tqdm(total=len(all_titles), desc="Phase 1", unit="sp")
        english_data = _run_phase1(all_titles, title_to_sci, target_locales, pbar1)
        pbar1.close()

        found = len(english_data)
        not_found = len(all_titles) - found
        print(f"  Found: {found}, not found: {not_found}")

        # Save after Phase 1 so English data isn't lost
        _assemble_records(work, english_data, {}, {}, existing)
        save_json(existing, OUTPUT_FILE)
        print(f"  Saved {found} records after Phase 1")
    else:
        print(f"\nPhase 1: No new species to fetch")

    if is_shutting_down():
        return

    # ── Phase 1b: Backfill missing English extracts ───────────────────
    pbar1b = tqdm(total=0, desc="Phase 1b", unit="sp")
    backfilled = _run_extract_backfill(existing, pbar1b)
    if backfilled:
        save_json(existing, OUTPUT_FILE)
        print(f"  Backfilled {backfilled} English extracts")
    else:
        print(f"  No missing English extracts to backfill")

    if is_shutting_down():
        return

    queued_species = {s for s, _ in work}
    incomplete = _collect_incomplete_work(species, existing, queued_species)
    if args.limit:
        remaining = max(0, args.limit - len(work))
        incomplete = incomplete[:remaining]

    # Reconstruct english_data for incomplete species from existing records
    # so Phase 2/3 can generate work for them
    for sci, wiki_title in incomplete:
        if wiki_title in english_data:
            continue  # already handled
        rec = existing.get(sci, {})
        if not rec.get("extract"):
            continue
        # Rebuild the english_data entry from the stored record
        langlinks = {}
        for lang, url in rec.get("wikipedia_urls", {}).items():
            if lang == "en":
                continue
            # Extract title from URL
            parsed = urlparse(url)
            if "/wiki/" in parsed.path:
                loc_title = unquote(parsed.path.split("/wiki/")[-1])
                langlinks[lang] = {"url": url, "title": loc_title}
        english_data[wiki_title] = {
            "title": rec.get("title", wiki_title),
            "extract": rec.get("extract", ""),
            "description": rec.get("description", ""),
            "image_url": rec.get("image_url", ""),
            "langlinks": langlinks,
        }

    all_work = work + incomplete

    # Build lookup of existing extracts per wiki_title (for Phase 2 skip)
    existing_extracts: dict[str, dict[str, str]] = {}
    for sci, wiki_title in all_work:
        rec = existing.get(sci, {})
        exts = rec.get("extracts", {})
        if exts:
            existing_extracts[wiki_title] = exts

    # Build set of wiki_titles that already have image license (for Phase 3 skip)
    existing_licenses: set[str] = set()
    for sci, wiki_title in all_work:
        rec = existing.get(sci, {})
        if rec.get("image_license"):
            existing_licenses.add(wiki_title)

    # ── Phase 2: Locale extracts ──────────────────────────────────────
    total_locale_items = sum(
        len(d.get("langlinks", {}))
        for d in english_data.values()
    )
    print(f"\nPhase 2: Locale extracts ({total_locale_items} total langlinks)...")
    pbar2 = tqdm(total=total_locale_items, desc="Phase 2", unit="ext")
    locale_extracts = _run_phase2(english_data, existing_extracts, pbar2)
    pbar2.close()

    # Save after Phase 2 so locale extracts aren't lost
    _assemble_records(all_work, english_data, locale_extracts, {}, existing)
    save_json(existing, OUTPUT_FILE)
    n_new_extracts = sum(len(v) for v in locale_extracts.values())
    print(f"  Saved {n_new_extracts} new locale extracts after Phase 2")

    if is_shutting_down():
        return

    # ── Phase 3: Image licenses ───────────────────────────────────────
    n_images = sum(1 for d in english_data.values() if d.get("image_url"))
    n_need_license = n_images - len(existing_licenses)
    img_batches = (n_need_license + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\nPhase 3: Image licenses ({n_need_license} need licenses, "
          f"{len(existing_licenses)} already done, ~{img_batches} batches)...")
    pbar3 = tqdm(total=n_need_license, desc="Phase 3", unit="img")
    title_licenses = _run_phase3(english_data, existing_licenses, pbar3)
    pbar3.close()

    # ── Final save (with image licenses) ──────────────────────────────
    _assemble_records(all_work, english_data, locale_extracts, title_licenses, existing)
    save_json(existing, OUTPUT_FILE)

    success = sum(1 for _, t in all_work if t in english_data)
    print(f"\nDone! {success}/{len(all_work)} species with summaries,"
          f" {n_new_extracts} new locale extracts,"
          f" {len(title_licenses)} new image licenses.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries")


if __name__ == "__main__":
    main()
