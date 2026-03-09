#!/usr/bin/env python3
"""
Build the unified species metadata file.

Combines two phases:
  Phase 1 — Taxonomy: Cross-reference iNaturalist, AviList, and Wikidata to
            build a canonical species list with common names, images, and
            external identifiers.
  Phase 2 — Merge: Enrich each species with a single description using the
            priority Claude > Wikipedia > eBird, then write the final output.

Input files (all in raw_data/):
  - inat_data.json         (from collectors/inat.py)
  - ebird_data.json        (from collectors/ebird.py)
  - wikipedia_data.json    (from collectors/wikipedia.py)
  - claude_data.json       (from collectors/claude.py — optional)
  - AviList CSV            (from collectors/avilist.py)

Output:
  - dist/species_metadata.json
  - dist/species_metadata.csv
  - dist/species_metadata.zip

Usage:
    python -m build.metadata [--merge-only] [--dry-run] [--no-zip] [--dev]
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

from config import load_config
from collectors._common import ROOT, RAW_DIR, USER_AGENT, load_json, save_json

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
EBIRD_TAXONOMY_URL = "https://api.ebird.org/v2/ref/taxonomy/ebird"

INAT_FILE = RAW_DIR / "inat_data.json"
EBIRD_DATA_FILE = RAW_DIR / "ebird_data.json"
WIKI_DATA_FILE = RAW_DIR / "wikipedia_data.json"
CLAUDE_DATA_FILE = RAW_DIR / "claude_data.json"
TAXONOMY_FILE = RAW_DIR / "taxonomy.json"

# Acceptable iNat photo licenses (NC is fine; only "all rights reserved" rejected)
ACCEPTABLE_INAT_LICENSES = {
    "cc0", "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa",
    "cc-by-nd", "cc-by-nc-nd", "pd", "gfdl",
}

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_COMMONS_BATCH = 50
_SPARQL_BATCH = 150

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

# Normalize locale codes across sources to canonical forms.
LOCALE_NORMALIZE: dict[str, str] = {
    "nb": "no",
    "pt-br": "pt",
}

# Wikidata properties for species identifiers
WD_IDENTIFIERS = {
    "ebird_code": "P3444",
    "gbif_id": "P846",
    "ncbi_id": "P685",
    "avibase_id": "P2426",
    "birdlife_id": "P5257",
}


# ---------------------------------------------------------------------------
# Wikidata / eBird helpers
# ---------------------------------------------------------------------------

def _sparql_query(query: str) -> list[dict]:
    """Run a SPARQL query against Wikidata (POST to avoid URL limits)."""
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        WIKIDATA_SPARQL, data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())["results"]["bindings"]
    except Exception as e:
        print(f"  WARNING: Wikidata query failed: {e}")
        return []


def query_wikidata_ebird(unmatched: list[tuple[str, int]]) -> dict[str, str]:
    """Query Wikidata for eBird taxon IDs of unmatched species."""
    if not unmatched:
        return {}

    results = {}

    # Pass A: by scientific name (P225 → P3444)
    for i in range(0, len(unmatched), _SPARQL_BATCH):
        batch = unmatched[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{sci}"' for sci, _ in batch)
        rows = _sparql_query(
            f"SELECT ?taxonName ?ebirdId WHERE {{"
            f"  VALUES ?taxonName {{ {values} }}"
            f"  ?item wdt:P225 ?taxonName ."
            f"  ?item wdt:P3444 ?ebirdId ."
            f"}}"
        )
        for r in rows:
            results[r["taxonName"]["value"]] = r["ebirdId"]["value"]

    # Pass B: remaining — by iNaturalist taxon ID (P3151 → P3444)
    remaining = [(sci, iid) for sci, iid in unmatched
                 if sci not in results and iid is not None]
    if remaining:
        iid_to_sci = {str(iid): sci for sci, iid in remaining}
        for i in range(0, len(remaining), _SPARQL_BATCH):
            batch = remaining[i:i + _SPARQL_BATCH]
            inat_values = " ".join(f'"{iid}"' for _, iid in batch)
            rows = _sparql_query(
                f"SELECT ?inatId ?ebirdId WHERE {{"
                f"  VALUES ?inatId {{ {inat_values} }}"
                f"  ?item wdt:P3151 ?inatId ."
                f"  ?item wdt:P3444 ?ebirdId ."
                f"}}"
            )
            for r in rows:
                iid = r["inatId"]["value"]
                sci = iid_to_sci.get(iid)
                if sci and sci not in results:
                    results[sci] = r["ebirdId"]["value"]

    return results


def query_wikidata_identifiers(species_names: list[str]) -> dict[str, dict]:
    """Batch-query Wikidata for external identifiers (GBIF, NCBI, etc.)."""
    if not species_names:
        return {}

    optionals = ""
    selects = ["?taxonName"]
    for key, prop in WD_IDENTIFIERS.items():
        if key == "ebird_code":
            continue
        var = f"?{key}"
        selects.append(var)
        optionals += f"  OPTIONAL {{ ?item wdt:{prop} {var} . }}\n"

    results = {}
    select_str = " ".join(selects)

    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        query = (
            f"SELECT {select_str} WHERE {{\n"
            f"  VALUES ?taxonName {{ {values} }}\n"
            f"  ?item wdt:P225 ?taxonName .\n"
            f"{optionals}}}"
        )
        rows = _sparql_query(query)
        for r in rows:
            sci = r["taxonName"]["value"]
            ids = {}
            for key in WD_IDENTIFIERS:
                if key == "ebird_code":
                    continue
                val = r.get(key, {}).get("value", "")
                if val:
                    ids[key] = val
            if ids:
                results[sci] = ids

    return results


def _download_ebird_taxonomy(locale: str) -> dict[str, str]:
    """Download eBird taxonomy CSV for a locale → {code: name}."""
    url = f"{EBIRD_TAXONOMY_URL}?fmt=csv&locale={locale}&cat=species"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(data))
        return {row["SPECIES_CODE"]: row["COMMON_NAME"]
                for row in reader if row.get("SPECIES_CODE")}
    except Exception as e:
        print(f"  WARNING: eBird taxonomy download failed for {locale}: {e}")
        return {}


def fetch_ebird_names() -> dict[str, dict[str, str]]:
    """Download eBird taxonomy for ALL available locales.

    Returns {species_code: {canonical_locale: common_name}}.
    Only includes actual translations (skips English fallbacks).
    """
    print("    Downloading English baseline...")
    en_names = _download_ebird_taxonomy("en")
    if not en_names:
        print("  WARNING: Could not download eBird English taxonomy.")
        return {}

    result: dict[str, dict[str, str]] = {}
    for code, name in en_names.items():
        result.setdefault(code, {})["en"] = name

    for ebird_loc, canonical in EBIRD_LOCALES:
        print(f"    Downloading {canonical} (eBird: {ebird_loc})...",
              end=" ", flush=True)
        names = _download_ebird_taxonomy(ebird_loc)
        translated = 0
        for code, name in names.items():
            en_name = en_names.get(code, "")
            if name and name != en_name:
                result.setdefault(code, {})[canonical] = name
                translated += 1
        print(f"{translated}/{len(names)} translated")
        time.sleep(0.2)

    return result


def query_wikidata_labels(
    species_names: list[str],
) -> dict[str, dict[str, str]]:
    """Query Wikidata for species labels in ALL languages."""
    if not species_names:
        return {}

    results: dict[str, dict[str, str]] = {}

    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        query = (
            f"SELECT ?taxonName ?label WHERE {{\n"
            f"  VALUES ?taxonName {{ {values} }}\n"
            f"  ?item wdt:P225 ?taxonName .\n"
            f"  ?item rdfs:label ?label .\n"
            f"  FILTER(STRLEN(LANG(?label)) >= 2)\n"
            f"}}"
        )
        rows = _sparql_query(query)
        for r in rows:
            sci = r["taxonName"]["value"]
            label = r["label"]["value"]
            lang = r["label"].get("xml:lang", "")
            canonical = LOCALE_NORMALIZE.get(lang, lang)
            if label == sci:
                continue
            if canonical not in results.get(sci, {}):
                results.setdefault(sci, {})[canonical] = label

    return results


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_image_author(attribution: str, source: str = "") -> str:
    """Extract author name from attribution strings."""
    if not attribution:
        return ""
    text = attribution.strip()

    if source == "ebird" or (" - " in text and not text.startswith("(")):
        text = text.split(" - ", 1)[-1].strip()
        text = re.sub(r"\s*/\s*Macaulay Library.*", "", text, flags=re.IGNORECASE)
        return text.strip() if text else ""

    if source == "inat" or text.startswith("(c)"):
        text = re.sub(r"^\(c\)\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r",?\s*(some|all)\s+rights\s+reserved.*", "", text,
                       flags=re.IGNORECASE)
        text = re.sub(r"\(CC[^)]*\)", "", text)
        return text.strip(" ,.") if text.strip(" ,.") else ""

    if source == "wikimedia":
        return text

    return text


def _commons_url(filename: str) -> str:
    filename = filename.replace(" ", "_")
    md5 = hashlib.md5(filename.encode("utf-8")).hexdigest()
    encoded = urllib.parse.quote(filename, safe="")
    return (f"https://upload.wikimedia.org/wikipedia/commons/"
            f"{md5[0]}/{md5[:2]}/{encoded}")


def _is_commons_license_ok(license_short: str) -> bool:
    if not license_short:
        return False
    low = license_short.lower()
    return any(x in low for x in ("cc", "public domain", "pd", "gfdl"))


def query_wikidata_images(species_names: list[str]) -> dict[str, str]:
    """Query Wikidata P18 (image) → {scientific_name: Commons filename}."""
    if not species_names:
        return {}

    results: dict[str, str] = {}
    for i in range(0, len(species_names), _SPARQL_BATCH):
        batch = species_names[i:i + _SPARQL_BATCH]
        values = " ".join(f'"{s}"' for s in batch)
        rows = _sparql_query(
            f"SELECT ?taxonName ?image WHERE {{\n"
            f"  VALUES ?taxonName {{ {values} }}\n"
            f"  ?item wdt:P225 ?taxonName .\n"
            f"  ?item wdt:P18 ?image .\n"
            f"}}"
        )
        for r in rows:
            sci = r["taxonName"]["value"]
            image_url = r["image"]["value"]
            filename = urllib.parse.unquote(image_url.split("/")[-1])
            if sci not in results:
                results[sci] = filename

    return results


def check_commons_licenses(
    filenames: dict[str, str],
) -> dict[str, dict]:
    """Batch-check licenses for Wikimedia Commons files."""
    if not filenames:
        return {}

    file_to_sci: dict[str, list[str]] = {}
    for sci, fn in filenames.items():
        norm = fn.replace(" ", "_")
        file_to_sci.setdefault(norm, []).append(sci)

    all_files = list(file_to_sci.keys())
    results: dict[str, dict] = {}

    for i in range(0, len(all_files), _COMMONS_BATCH):
        batch = all_files[i:i + _COMMONS_BATCH]
        titles = "|".join(f"File:{fn}" for fn in batch)
        post_data = urllib.parse.urlencode({
            "action": "query",
            "titles": titles,
            "prop": "imageinfo",
            "iiprop": "extmetadata",
            "format": "json",
        }).encode("utf-8")
        req = urllib.request.Request(
            _COMMONS_API, data=post_data,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  WARNING: Commons license check failed: {e}")
            continue

        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            title = page.get("title", "")
            if not title.startswith("File:"):
                continue
            fn = title[5:]

            imageinfo = page.get("imageinfo", [])
            if not imageinfo:
                continue
            meta = imageinfo[0].get("extmetadata", {})

            license_short = meta.get("LicenseShortName", {}).get("value", "")
            license_url = meta.get("LicenseUrl", {}).get("value", "")
            artist_html = meta.get("Artist", {}).get("value", "")
            artist = _strip_html_tags(artist_html) if artist_html else ""

            if not _is_commons_license_ok(license_short):
                continue

            url = _commons_url(fn)
            norm = fn.replace(" ", "_")
            for sci in file_to_sci.get(norm, []):
                results[sci] = {
                    "url": url,
                    "attribution": artist,
                    "license": license_short,
                    "license_url": license_url,
                }

    return results


def load_ebird_images() -> dict[str, dict]:
    """Load eBird image data from ebird_data.json → {ebird_code: {url, attribution}}."""
    if not EBIRD_DATA_FILE.exists():
        return {}
    try:
        with open(EBIRD_DATA_FILE, encoding="utf-8") as f:
            ebird = json.load(f)
    except Exception:
        return {}

    result: dict[str, dict] = {}
    for sci, rec in ebird.items():
        code = rec.get("ebird_code", "")
        url = rec.get("image_url", "")
        attr = rec.get("image_attribution", "")
        if code and url:
            result[code] = {"url": url, "attribution": attr}
    return result


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------

def load_inat() -> dict:
    if not INAT_FILE.exists():
        print(f"ERROR: {INAT_FILE.name} not found. Run: python -m collectors.inat")
        raise SystemExit(1)
    return load_json(INAT_FILE)


def load_avilist(cfg: dict) -> list[dict]:
    csv_name = cfg.get("avilist", {}).get("csv_file", "")
    csv_path = RAW_DIR / csv_name if csv_name else None
    if not csv_path or not csv_path.exists():
        print(f"WARNING: AviList CSV not found at {csv_path}. "
              "Birds will not have eBird codes. Run: python -m collectors.avilist")
        return []

    rows = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("Taxon_rank") != "species":
                continue
            sci = (row.get("Scientific_name") or "").strip()
            code = (row.get("Species_code_Cornell_Lab") or "").strip()
            en_clements = (row.get("English_name_Clements_v2024") or "").strip()
            en_avilist = (row.get("English_name_AviList") or "").strip()
            if sci and code:
                rows.append({
                    "scientific_name": sci,
                    "ebird_code": code,
                    "common_name_clements": en_clements,
                    "common_name_avilist": en_avilist,
                })
    return rows


# ---------------------------------------------------------------------------
# Phase 1: Build taxonomy
# ---------------------------------------------------------------------------

def build_taxonomy(inat: dict, avilist_rows: list[dict]) -> tuple[dict, dict]:
    """Cross-reference iNat and AviList to build a unified taxonomy.

    Returns (taxonomy_dict, stats_dict).
    """
    taxonomy = {}

    avi_by_sci = {}
    avi_by_en = {}
    for row in avilist_rows:
        avi_by_sci[row["scientific_name"]] = row
        for name in (row["common_name_clements"], row["common_name_avilist"]):
            if name:
                avi_by_en[name.lower()] = row

    matched_sci = set()
    stats = {"direct": 0, "common_name": 0, "wikidata": 0, "inat_only": 0,
             "avilist_only": 0, "non_bird": 0}
    pending_unmatched = []

    # Pass 1: Process all iNat species
    for sci_name, rec in inat.items():
        if rec.get("inat_id") is None:
            continue

        is_bird = rec.get("taxon_group") == "Aves"
        ebird_code = ""
        match_source = ""

        if is_bird:
            avi_row = avi_by_sci.get(sci_name)
            if avi_row:
                ebird_code = avi_row["ebird_code"]
                match_source = "direct"
                matched_sci.add(avi_row["scientific_name"])
                stats["direct"] += 1
            else:
                cn = (rec.get("preferred_common_name") or "").lower()
                avi_row = avi_by_en.get(cn) if cn else None
                if avi_row and avi_row["scientific_name"] not in matched_sci:
                    ebird_code = avi_row["ebird_code"]
                    match_source = "common_name"
                    matched_sci.add(avi_row["scientific_name"])
                    stats["common_name"] += 1
                else:
                    match_source = "inat_only"
                    pending_unmatched.append((sci_name, rec["inat_id"]))
        else:
            match_source = "non_bird"
            stats["non_bird"] += 1

        taxonomy[sci_name] = {
            "inat_id": rec["inat_id"],
            "taxon_group": rec.get("taxon_group", ""),
            "iconic_taxon_name": rec.get("iconic_taxon_name", ""),
            "preferred_common_name": rec.get("preferred_common_name", ""),
            "common_names": {
                LOCALE_NORMALIZE.get(k, k): v
                for k, v in rec.get("common_names", {}).items()
            },
            "observations_count": rec.get("observations_count", 0),
            "ebird_code": ebird_code,
            "gbif_id": "",
            "ncbi_id": "",
            "avibase_id": "",
            "birdlife_id": "",
            "image_url": "",
            "image_author": "",
            "image_license": "",
            "image_source": "",
            "match_source": match_source,
        }

    # Pass 1b: Wikidata lookup for unmatched birds
    if pending_unmatched:
        print(f"  Querying Wikidata for {len(pending_unmatched)} unmatched birds...")
        wd_results = query_wikidata_ebird(pending_unmatched)
        for sci_name, _ in pending_unmatched:
            ebird_code = wd_results.get(sci_name, "")
            if ebird_code:
                taxonomy[sci_name]["ebird_code"] = ebird_code
                taxonomy[sci_name]["match_source"] = "wikidata"
                stats["wikidata"] += 1
            else:
                stats["inat_only"] += 1

    # Pass 2: Add AviList-only species
    for row in avilist_rows:
        if row["scientific_name"] in matched_sci:
            continue
        if row["scientific_name"] in taxonomy:
            continue

        sci = row["scientific_name"]
        en = row["common_name_clements"] or row["common_name_avilist"] or ""
        taxonomy[sci] = {
            "inat_id": None,
            "taxon_group": "Aves",
            "iconic_taxon_name": "Aves",
            "preferred_common_name": en,
            "common_names": {"en": en} if en else {},
            "observations_count": 0,
            "ebird_code": row["ebird_code"],
            "gbif_id": "",
            "ncbi_id": "",
            "avibase_id": "",
            "birdlife_id": "",
            "image_url": "",
            "image_author": "",
            "image_license": "",
            "image_source": "",
            "match_source": "avilist_only",
        }
        stats["avilist_only"] += 1

    # Pass 3: Wikidata identifiers (GBIF, NCBI, etc.)
    all_names = list(taxonomy.keys())
    n_batches = (len(all_names) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
    print(f"  Querying Wikidata for identifiers ({len(all_names)} species, "
          f"{n_batches} batches)...")
    wd_ids = query_wikidata_identifiers(all_names)
    id_counts = {k: 0 for k in WD_IDENTIFIERS if k != "ebird_code"}
    for sci, ids in wd_ids.items():
        if sci in taxonomy:
            for key, val in ids.items():
                taxonomy[sci][key] = val
                id_counts[key] += 1
    stats["wikidata_ids"] = id_counts
    stats["wikidata_coverage"] = len(wd_ids)

    # Pass 4: eBird common names (authority for bird names)
    print(f"\n  Fetching eBird common names ({len(EBIRD_LOCALES)} locales)...")
    ebird_names = fetch_ebird_names()
    ebird_name_counts: dict[str, int] = {}
    for sci, entry in taxonomy.items():
        code = entry.get("ebird_code", "")
        if not code:
            continue
        names = ebird_names.get(code, {})
        for loc, name in names.items():
            entry["common_names"][loc] = name
            ebird_name_counts[loc] = ebird_name_counts.get(loc, 0) + 1
    stats["ebird_names"] = ebird_name_counts

    # Pass 5: Wikidata labels as fallback for all species
    n_label_batches = (len(all_names) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
    print(f"\n  Querying Wikidata for labels ({len(all_names)} species, "
          f"all languages, {n_label_batches} batches)...")
    wd_labels = query_wikidata_labels(all_names)
    wd_label_counts: dict[str, int] = {}
    for sci, labels in wd_labels.items():
        if sci in taxonomy:
            for loc, label in labels.items():
                if loc not in taxonomy[sci]["common_names"]:
                    taxonomy[sci]["common_names"][loc] = label
                    wd_label_counts[loc] = wd_label_counts.get(loc, 0) + 1
    stats["wikidata_labels"] = wd_label_counts

    # Pass 6: Default image selection (iNat → Wikimedia Commons → eBird)
    img_stats = {"inat": 0, "wikimedia": 0, "ebird": 0, "none": 0}

    print("\n  Selecting default images...")
    for sci, entry in taxonomy.items():
        inat_rec = inat.get(sci, {})
        url = inat_rec.get("image_url", "")
        lic = inat_rec.get("image_license", "")
        if url and lic in ACCEPTABLE_INAT_LICENSES:
            entry["image_url"] = url
            entry["image_author"] = _parse_image_author(
                inat_rec.get("image_attribution", ""), "inat")
            entry["image_license"] = lic
            entry["image_source"] = "inat"
            img_stats["inat"] += 1

    need_image = [sci for sci, e in taxonomy.items() if not e["image_url"]]
    if need_image:
        n_img_batches = (len(need_image) + _SPARQL_BATCH - 1) // _SPARQL_BATCH
        print(f"    Querying Wikidata P18 for {len(need_image)} species "
              f"({n_img_batches} batches)...")
        wd_images = query_wikidata_images(need_image)
        print(f"    Found {len(wd_images)} Commons images, checking licenses...")
        commons_results = check_commons_licenses(wd_images)
        for sci, info in commons_results.items():
            if sci in taxonomy and not taxonomy[sci]["image_url"]:
                taxonomy[sci]["image_url"] = info["url"]
                taxonomy[sci]["image_author"] = info["attribution"]
                taxonomy[sci]["image_license"] = info["license"]
                taxonomy[sci]["image_source"] = "wikimedia"
                img_stats["wikimedia"] += 1

    ebird_imgs = load_ebird_images()
    if ebird_imgs:
        for sci, entry in taxonomy.items():
            if entry["image_url"]:
                continue
            code = entry.get("ebird_code", "")
            if code and code in ebird_imgs:
                eb = ebird_imgs[code]
                entry["image_url"] = eb["url"]
                entry["image_author"] = _parse_image_author(
                    eb["attribution"], "ebird")
                entry["image_license"] = ""
                entry["image_source"] = "ebird"
                img_stats["ebird"] += 1

    img_stats["none"] = sum(1 for e in taxonomy.values() if not e["image_url"])
    stats["images"] = img_stats

    # Coverage stats
    all_locales: set[str] = set()
    for entry in taxonomy.values():
        all_locales.update(entry.get("common_names", {}).keys())
    coverage: dict[str, int] = {}
    for entry in taxonomy.values():
        for loc in all_locales:
            if loc in entry.get("common_names", {}):
                coverage[loc] = coverage.get(loc, 0) + 1
    stats["coverage"] = coverage

    return taxonomy, stats


def print_taxonomy_stats(taxonomy: dict, stats: dict):
    """Print summary statistics for the taxonomy build."""
    total_birds = (stats["direct"] + stats["common_name"] + stats["wikidata"]
                   + stats["inat_only"] + stats["avilist_only"])
    matched_birds = stats["direct"] + stats["common_name"] + stats["wikidata"]
    inat_birds = matched_birds + stats["inat_only"]
    total = len(taxonomy)

    print(f"\n  Total species: {total}")
    print(f"  Birds:         {total_birds}")
    print(f"    Direct match:     {stats['direct']}")
    print(f"    Common name:      {stats['common_name']}")
    print(f"    Wikidata:         {stats['wikidata']}")
    print(f"    iNat only:        {stats['inat_only']} (no eBird code)")
    print(f"    AviList only:     {stats['avilist_only']} (no iNat ID)")
    print(f"    Match rate:       {matched_birds}/{inat_birds} "
          f"iNat birds ({100 * matched_birds / max(1, inat_birds):.1f}%)")
    print(f"  Non-birds:     {stats['non_bird']}")

    # Wikidata identifiers
    wd_ids = stats.get("wikidata_ids", {})
    wd_cov = stats.get("wikidata_coverage", 0)
    print(f"\n  Wikidata identifiers ({wd_cov}/{total} species found):")
    for key, count in wd_ids.items():
        print(f"    {key}: {count}")

    # Common name coverage
    coverage = stats.get("coverage", {})
    ebird_n = stats.get("ebird_names", {})
    wd_labels = stats.get("wikidata_labels", {})
    n_locales = len(coverage)
    top_n = 30
    print(f"\n  Common name coverage ({total} species, "
          f"{n_locales} locales, top {top_n}):")
    print(f"    {'Locale':<8} {'Total':>7} {'%':>6}  "
          f"{'eBird':>7} {'Wikidata':>8}")
    sorted_locs = sorted(coverage.keys(), key=lambda x: -coverage.get(x, 0))
    for loc in sorted_locs[:top_n]:
        cov = coverage.get(loc, 0)
        eb = ebird_n.get(loc, 0)
        wd = wd_labels.get(loc, 0)
        pct = 100 * cov / max(1, total)
        print(f"    {loc:<8} {cov:>7} {pct:>5.1f}%  {eb:>7} {wd:>8}")
    if n_locales > top_n:
        print(f"    ... and {n_locales - top_n} more locales")

    # Images
    img = stats.get("images", {})
    total_with_img = img.get("inat", 0) + img.get("wikimedia", 0) + img.get("ebird", 0)
    print(f"\n  Default images ({total_with_img}/{total} species):")
    print(f"    iNat (permissive):  {img.get('inat', 0)}")
    print(f"    Wikimedia Commons:  {img.get('wikimedia', 0)}")
    print(f"    eBird:              {img.get('ebird', 0)}")
    print(f"    No image:           {img.get('none', 0)}")


# ---------------------------------------------------------------------------
# Phase 2: Merge into final metadata
# ---------------------------------------------------------------------------

def build_metadata(taxonomy: dict, ebird: dict, wiki: dict,
                   claude: dict) -> list[dict]:
    """Merge taxonomy + raw sources into final species records.

    Each record:
      scientific_name, common_name, taxon_group, common_names,
      description, image_url, image_author, image_license, image_source,
      inat_id, ebird_code, gbif_id, ncbi_id, avibase_id, birdlife_id,
      observations_count
    """
    records = []
    desc_sources: dict[str, int] = Counter()

    for sci_name, tax in taxonomy.items():
        cl = claude.get(sci_name, {})
        wp = wiki.get(sci_name, {})
        eb = ebird.get(sci_name, {})

        description = ""
        description_source = ""
        if cl.get("description_en"):
            description = cl["description_en"]
            description_source = "claude"
            desc_sources["claude"] += 1
        elif wp.get("extract"):
            description = wp["extract"]
            description_source = "wikipedia"
            desc_sources["wikipedia"] += 1
        elif eb.get("description"):
            description = eb["description"]
            description_source = "ebird"
            desc_sources["ebird"] += 1
        else:
            desc_sources["none"] += 1

        record = {
            "scientific_name": sci_name,
            "common_name": tax.get("preferred_common_name", ""),
            "taxon_group": tax.get("taxon_group", ""),
            "common_names": tax.get("common_names", {}),
            "description": description,
            "description_source": description_source,
            "image_url": tax.get("image_url", ""),
            "image_author": tax.get("image_author", ""),
            "image_license": tax.get("image_license", ""),
            "image_source": tax.get("image_source", ""),
            "inat_id": tax.get("inat_id"),
            "ebird_code": tax.get("ebird_code", ""),
            "gbif_id": tax.get("gbif_id", ""),
            "ncbi_id": tax.get("ncbi_id", ""),
            "avibase_id": tax.get("avibase_id", ""),
            "birdlife_id": tax.get("birdlife_id", ""),
            "observations_count": tax.get("observations_count", 0),
        }
        records.append(record)

    # Sort: taxon group, then observations descending
    group_order = {"Aves": 0, "Mammalia": 1, "Reptilia": 2,
                   "Amphibia": 3, "Insecta": 4}
    records.sort(key=lambda r: (
        group_order.get(r["taxon_group"], 99),
        -r["observations_count"],
    ))

    # Print stats
    groups = Counter(r["taxon_group"] for r in records)
    n_locales = len(set(
        loc for r in records for loc in r.get("common_names", {}).keys()
    ))
    img_sources = Counter(r.get("image_source", "") or "none" for r in records)

    print(f"  {len(records)} species")
    for g, n in sorted(groups.items()):
        print(f"    {g}: {n}")

    print(f"\n  Common names: {n_locales} locales")

    print(f"\n  Descriptions:")
    for src, cnt in desc_sources.most_common():
        print(f"    {src}: {cnt}")

    print(f"\n  Images:")
    for src, cnt in img_sources.most_common():
        print(f"    {src}: {cnt}")

    return records


def records_to_csv(records: list[dict]) -> str:
    """Convert records to CSV (top 30 locales as separate columns)."""
    locale_counts: Counter[str] = Counter()
    for r in records:
        locale_counts.update(r.get("common_names", {}).keys())
    top_locales = [loc for loc, _ in locale_counts.most_common(30)]

    base_cols = [
        "scientific_name", "common_name", "taxon_group",
        "description", "description_source",
        "image_url", "image_author", "image_license", "image_source",
        "inat_id", "ebird_code", "gbif_id", "ncbi_id",
        "avibase_id", "birdlife_id", "observations_count",
    ]
    locale_cols = [f"common_name_{loc}" for loc in top_locales]
    fieldnames = base_cols + locale_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for rec in records:
        row = {k: rec.get(k, "") for k in base_cols}
        for loc in top_locales:
            row[f"common_name_{loc}"] = rec.get("common_names", {}).get(loc, "")
        writer.writerow(row)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build unified species metadata from all data sources"
    )
    parser.add_argument("--merge-only", action="store_true",
                        help="Skip taxonomy rebuild; use existing taxonomy.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show stats without writing output")
    parser.add_argument("--dev", action="store_true",
                        help="Write to dev/ instead of dist/")
    parser.add_argument("--no-zip", action="store_true",
                        help="Skip zip archive creation")
    args = parser.parse_args()

    cfg = load_config()

    # Phase 1: Build taxonomy (or load existing)
    if args.merge_only:
        print("Loading existing taxonomy...")
        taxonomy = load_json(TAXONOMY_FILE)
        if not taxonomy:
            print("ERROR: No taxonomy.json found. "
                  "Run without --merge-only to build it.")
            raise SystemExit(1)
        print(f"  Loaded {len(taxonomy)} species from taxonomy.json")
    else:
        print("Loading sources...")
        inat = load_inat()
        avilist_rows = load_avilist(cfg)
        print(f"  iNaturalist: {len(inat)} species")
        print(f"  AviList:     {len(avilist_rows)} bird species")

        print("\nBuilding taxonomy...")
        taxonomy, stats = build_taxonomy(inat, avilist_rows)
        print_taxonomy_stats(taxonomy, stats)

        if not args.dry_run:
            save_json(taxonomy, TAXONOMY_FILE)
            size_mb = TAXONOMY_FILE.stat().st_size / 1024 / 1024
            print(f"\n  Saved: {TAXONOMY_FILE} ({size_mb:.1f} MB)")

    if args.dry_run:
        # Show unmatched examples
        if not args.merge_only:
            unmatched = [(k, v) for k, v in taxonomy.items()
                         if v["taxon_group"] == "Aves"
                         and v["match_source"] == "inat_only"]
            if unmatched:
                unmatched.sort(key=lambda x: -x[1].get("observations_count", 0))
                print(f"\n  Top unmatched iNat birds (by observations):")
                for sci, rec in unmatched[:15]:
                    cn = rec.get("preferred_common_name", "")
                    obs = rec.get("observations_count", 0)
                    print(f"    {sci} — {cn} ({obs:,} obs)")
        return

    # Phase 2: Merge with descriptions → final output
    out_dir = ROOT / ("dev" if args.dev else "dist")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\nLoading enrichment data...")
    ebird = load_json(EBIRD_DATA_FILE)
    wiki = load_json(WIKI_DATA_FILE)
    claude = load_json(CLAUDE_DATA_FILE)
    print(f"  eBird:     {len(ebird):>8} species")
    print(f"  Wikipedia: {len(wiki):>8} species")
    print(f"  Claude:    {len(claude):>8} species")

    print("\nMerging...")
    records = build_metadata(taxonomy, ebird, wiki, claude)

    # Write JSON (atomic)
    json_path = out_dir / "species_metadata.json"
    tmp = json_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, json_path)
    json_size = json_path.stat().st_size
    print(f"\n  JSON: {json_path} ({json_size / 1024 / 1024:.1f} MB)")

    # Write CSV (atomic)
    csv_path = out_dir / "species_metadata.csv"
    csv_text = records_to_csv(records)
    tmp = csv_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(csv_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, csv_path)
    csv_size = csv_path.stat().st_size
    print(f"  CSV:  {csv_path} ({csv_size / 1024 / 1024:.1f} MB)")

    # Zip
    if not args.no_zip:
        zip_path = out_dir / "species_metadata.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(json_path, "species_metadata.json")
            zf.write(csv_path, "species_metadata.csv")
        zip_size = zip_path.stat().st_size
        print(f"  ZIP:  {zip_path} ({zip_size / 1024 / 1024:.1f} MB)")

    print("\nDone!")


if __name__ == "__main__":
    main()
