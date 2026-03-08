#!/usr/bin/env python3
"""
Build a unified species taxonomy by cross-referencing iNaturalist and AviList.

Reads inat_data.json and AviList CSV, matches species across taxonomies,
and outputs a canonical taxonomy.json that downstream scripts (ebird.py,
wikipedia.py, merge.py) can use as their single source of truth.

Matching strategy for birds (in priority order):
  1. Direct scientific name match (iNat name == AviList name)
  2. Common name match (iNat English name == AviList Clements English name)
  3. Wikidata lookup (query eBird taxon ID via SPARQL, by sci name then iNat ID)
  4. iNat-only birds (no AviList/eBird match found)
  5. AviList-only birds (not in iNat — added with ebird_code, no inat_id)

Non-bird taxon groups (Mammalia, Insecta, etc.) pass through from iNat as-is.

Output: raw_data/taxonomy.json

Usage:
    python -m utils.taxonomy [--dry-run]
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
from pathlib import Path

from utils.config import load_config

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_UA = "BirdNET-SpeciesData/1.0 (taxonomy matching)"

EBIRD_TAXONOMY_URL = "https://api.ebird.org/v2/ref/taxonomy/ebird"

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw_data"
INAT_FILE = RAW / "inat_data.json"
EBIRD_DATA_FILE = RAW / "ebird_data.json"
OUTPUT_FILE = RAW / "taxonomy.json"

# Acceptable iNat licenses for default images.
# NC (non-commercial) is fine; ND (no-derivatives) is borderline but accepted.
# Only empty string ("all rights reserved") is rejected.
ACCEPTABLE_INAT_LICENSES = {
    "cc0", "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa",
    "cc-by-nd", "cc-by-nc-nd", "pd", "gfdl",
}

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_COMMONS_BATCH = 50  # MediaWiki API title limit

# All known eBird locales that have real translations (discovered 2026-03).
# Each entry is (ebird_locale, canonical_locale):
#   ebird_locale = the code passed to the eBird API
#   canonical_locale = the key stored in common_names dict
# Where they differ, the canonical code is used for storage (e.g. pt_BR → pt).
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
    ("pt_BR", "pt"), ("pt_PT", "pt_PT"),  # pt_BR is primary Portuguese
    ("ro", "ro"), ("ru", "ru"),
    ("sk", "sk"), ("sl", "sl"), ("sq", "sq"), ("sr", "sr"),
    ("sv", "sv"), ("te", "te"), ("th", "th"), ("tr", "tr"),
    ("uk", "uk"),
    ("zh_SIM", "zh"), ("zh_TRA", "zh_TRA"),  # zh_SIM is primary Chinese
    ("zu", "zu"),
]

# Normalize locale codes across all sources to canonical forms.
# Applied to iNat common_names keys and Wikidata label lang tags.
LOCALE_NORMALIZE: dict[str, str] = {
    "nb": "no",       # iNat + Wikidata use 'nb' (Bokmål) for Norwegian
    "pt-br": "pt",   # normalize hyphenated variants
}


def load_inat() -> dict:
    """Load iNaturalist species data."""
    if not INAT_FILE.exists():
        print(f"ERROR: {INAT_FILE.name} not found. Run utils/inat.py first.")
        raise SystemExit(1)
    with open(INAT_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_avilist(cfg: dict) -> list[dict]:
    """Load AviList CSV rows (species rank only)."""
    csv_name = cfg.get("avilist", {}).get("csv_file", "")
    csv_path = RAW / csv_name if csv_name else None
    if not csv_path or not csv_path.exists():
        print(f"WARNING: AviList CSV not found at {csv_path}. "
              "Birds will not have eBird codes. Run utils/avilist.py first.")
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


def _sparql_query(query: str) -> list[dict]:
    """Run a SPARQL query against the Wikidata endpoint (POST to avoid URL limits)."""
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        WIKIDATA_SPARQL,
        data=data,
        headers={
            "User-Agent": WIKIDATA_UA,
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


# Wikidata properties for species identifiers
WD_IDENTIFIERS = {
    "ebird_code": "P3444",    # eBird taxon ID
    "gbif_id": "P846",        # GBIF taxon ID
    "ncbi_id": "P685",        # NCBI taxonomy ID
    "avibase_id": "P2426",    # Avibase ID (birds)
    "birdlife_id": "P5257",   # BirdLife International ID (birds)
}

# Maximum species per SPARQL VALUES clause
_SPARQL_BATCH = 150


def query_wikidata_ebird(unmatched: list[tuple[str, int]]) -> dict[str, str]:
    """Query Wikidata for eBird taxon IDs of unmatched species.

    Args:
        unmatched: List of (scientific_name, inat_id) tuples.

    Returns:
        Dict mapping scientific_name -> ebird_code for species found in Wikidata.
    """
    if not unmatched:
        return {}

    results = {}

    # Pass A: query by scientific name (P225 -> P3444 eBird taxon ID)
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

    # Pass B: remaining species — query by iNaturalist taxon ID (P3151 -> P3444)
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
    """Batch-query Wikidata for external identifiers (GBIF, NCBI, etc.).

    Args:
        species_names: List of scientific names to look up.

    Returns:
        Dict mapping scientific_name -> {"gbif_id": ..., "ncbi_id": ..., ...}
        Only non-empty identifiers are included.
    """
    if not species_names:
        return {}

    # Build OPTIONAL clauses for each identifier
    optionals = ""
    selects = ["?taxonName"]
    for key, prop in WD_IDENTIFIERS.items():
        if key == "ebird_code":
            continue  # handled separately in query_wikidata_ebird
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
    """Download eBird taxonomy CSV for a locale.

    Returns:
        Dict mapping SPECIES_CODE -> COMMON_NAME.
    """
    url = (f"{EBIRD_TAXONOMY_URL}?fmt=csv"
           f"&locale={locale}&cat=species")
    req = urllib.request.Request(url, headers={"User-Agent": WIKIDATA_UA})
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

    eBird is the authority for bird common names. English names are
    fetched as a baseline to detect untranslated fallbacks.

    Returns:
        Dict mapping species_code -> {canonical_locale: common_name}.
        Only includes actual translations (skips English fallbacks).
    """
    # English baseline for detecting untranslated names
    print("    Downloading English baseline...")
    en_names = _download_ebird_taxonomy("en")
    if not en_names:
        print("  WARNING: Could not download eBird English taxonomy.")
        return {}

    result: dict[str, dict[str, str]] = {}

    # Add English names
    for code, name in en_names.items():
        result.setdefault(code, {})["en"] = name

    for ebird_loc, canonical in EBIRD_LOCALES:
        print(f"    Downloading {canonical} (eBird: {ebird_loc})...",
              end=" ", flush=True)
        names = _download_ebird_taxonomy(ebird_loc)
        translated = 0
        for code, name in names.items():
            en_name = en_names.get(code, "")
            if name and name != en_name:  # Only keep actual translations
                result.setdefault(code, {})[canonical] = name
                translated += 1
        print(f"{translated}/{len(names)} translated")
        time.sleep(0.2)  # Be gentle with the API

    return result


def query_wikidata_labels(
    species_names: list[str],
) -> dict[str, dict[str, str]]:
    """Query Wikidata for species labels (common names) in ALL languages.

    Args:
        species_names: Scientific names to look up.

    Returns:
        Dict mapping scientific_name -> {locale: label}.
        Locale codes are normalized via LOCALE_NORMALIZE.
        Skips labels that match the scientific name (not real translations)
        and filters to labels with >= 2 character lang tags (skips ''/'mul').
    """
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

            # Skip labels that equal the scientific name (not a real translation)
            if label == sci:
                continue

            # Don't overwrite — first result wins (iNat/eBird already populated)
            if canonical not in results.get(sci, {}):
                results.setdefault(sci, {})[canonical] = label

    return results


# ---------------------------------------------------------------------------
# Default image selection helpers
# ---------------------------------------------------------------------------

def _strip_html_tags(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_image_author(attribution: str, source: str = "") -> str:
    """Extract author name from attribution strings.

    Args:
        attribution: Raw attribution string from iNat, Wikimedia, or eBird.
        source: Hint for parsing format ("inat", "wikimedia", "ebird").

    Returns:
        Cleaned author name, or empty string if unparseable.
    """
    if not attribution:
        return ""
    text = attribution.strip()

    # eBird format: "Common Name - Photographer Name"
    if source == "ebird" or (" - " in text and not text.startswith("(")):
        text = text.split(" - ", 1)[-1].strip()
        # Remove trailing "/ Macaulay Library" etc.
        text = re.sub(r"\s*/\s*Macaulay Library.*", "", text, flags=re.IGNORECASE)
        return text.strip() if text else ""

    # iNat format: "(c) Author, some rights reserved (CC ...)"
    if source == "inat" or text.startswith("(c)"):
        text = re.sub(r"^\(c\)\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r",?\s*(some|all)\s+rights\s+reserved.*", "", text,
                       flags=re.IGNORECASE)
        text = re.sub(r"\(CC[^)]*\)", "", text)
        return text.strip(" ,.") if text.strip(" ,.") else ""

    # Wikimedia: already extracted as plain text by Commons API
    if source == "wikimedia":
        return text

    # Generic: return as-is
    return text


def _commons_url(filename: str) -> str:
    """Construct a Wikimedia Commons direct URL from a filename."""
    filename = filename.replace(" ", "_")
    md5 = hashlib.md5(filename.encode("utf-8")).hexdigest()
    encoded = urllib.parse.quote(filename, safe="")
    return (f"https://upload.wikimedia.org/wikipedia/commons/"
            f"{md5[0]}/{md5[:2]}/{encoded}")


def _is_commons_license_ok(license_short: str) -> bool:
    """Check if a Wikimedia Commons license is acceptable."""
    if not license_short:
        return False
    low = license_short.lower()
    return any(x in low for x in ("cc", "public domain", "pd", "gfdl"))


def query_wikidata_images(species_names: list[str]) -> dict[str, str]:
    """Query Wikidata P18 (image) for species.

    Returns dict mapping scientific_name -> Commons filename.
    """
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
            # P18 returns URL like .../Special:FilePath/Foo.jpg — extract filename
            filename = urllib.parse.unquote(image_url.split("/")[-1])
            if sci not in results:  # keep first result
                results[sci] = filename

    return results


def check_commons_licenses(
    filenames: dict[str, str],
) -> dict[str, dict]:
    """Batch-check licenses for Wikimedia Commons files.

    Args:
        filenames: dict mapping scientific_name -> Commons filename.

    Returns:
        dict mapping scientific_name -> {url, attribution, license, license_url}
        for files with acceptable licenses.
    """
    if not filenames:
        return {}

    # Invert: normalized_filename -> list of sci_names
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
            headers={"User-Agent": WIKIDATA_UA},
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
            fn = title[5:]  # strip "File:" prefix

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
            # Map back to scientific names
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
    """Load eBird image data from ebird_data.json if it exists.

    Returns dict mapping ebird_code -> {url, attribution}.
    """
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


def build_taxonomy(inat: dict, avilist_rows: list[dict]) -> dict:
    """Cross-reference iNat and AviList to build a unified taxonomy.

    Returns dict keyed by scientific_name with fields:
      - inat_id, taxon_group, iconic_taxon_name, preferred_common_name
      - common_names, observations_count
      - ebird_code (for birds, empty string for others)
      - gbif_id, ncbi_id, avibase_id, birdlife_id (from Wikidata, where available)
      - image_url, image_attribution, image_license, image_source
      - match_source: "direct" | "common_name" | "wikidata" | "inat_only" | "avilist_only"
    """
    taxonomy = {}

    # Build AviList lookup dicts
    avi_by_sci = {}   # scientific_name -> row
    avi_by_en = {}    # lower(common_name) -> row
    for row in avilist_rows:
        avi_by_sci[row["scientific_name"]] = row
        for name in (row["common_name_clements"], row["common_name_avilist"]):
            if name:
                avi_by_en[name.lower()] = row

    matched_sci = set()  # Track which AviList rows we've used
    stats = {"direct": 0, "common_name": 0, "wikidata": 0, "inat_only": 0,
             "avilist_only": 0, "non_bird": 0}
    pending_unmatched = []  # Collect (sci_name, inat_id) for Wikidata lookup

    # Pass 1: Process all iNat species
    for sci_name, rec in inat.items():
        if rec.get("inat_id") is None:
            continue

        is_bird = rec.get("taxon_group") == "Aves"
        ebird_code = ""
        match_source = ""

        if is_bird:
            # Try direct scientific name match
            avi_row = avi_by_sci.get(sci_name)
            if avi_row:
                ebird_code = avi_row["ebird_code"]
                match_source = "direct"
                matched_sci.add(avi_row["scientific_name"])
                stats["direct"] += 1
            else:
                # Try common name match
                cn = (rec.get("preferred_common_name") or "").lower()
                avi_row = avi_by_en.get(cn) if cn else None
                if avi_row and avi_row["scientific_name"] not in matched_sci:
                    ebird_code = avi_row["ebird_code"]
                    match_source = "common_name"
                    matched_sci.add(avi_row["scientific_name"])
                    stats["common_name"] += 1
                else:
                    # Mark as pending — will try Wikidata next
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

    # Pass 1b: Wikidata lookup for unmatched birds (eBird codes)
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

    # Pass 2: Add AviList-only species (birds not in iNat)
    for row in avilist_rows:
        if row["scientific_name"] in matched_sci:
            continue
        if row["scientific_name"] in taxonomy:
            continue  # Already added from iNat

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

    # Pass 3: Enrich all species with Wikidata identifiers (GBIF, NCBI, etc.)
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

    # Pass 4: eBird common names for birds (authority for bird names)
    print(f"\n  Fetching eBird common names ({len(EBIRD_LOCALES)} locales)...")
    ebird_names = fetch_ebird_names()
    ebird_name_counts: dict[str, int] = {}
    for sci, entry in taxonomy.items():
        code = entry.get("ebird_code", "")
        if not code:
            continue
        names = ebird_names.get(code, {})
        for loc, name in names.items():
            entry["common_names"][loc] = name  # eBird overwrites for birds
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

    # Pass 6: Default image selection
    # Priority: iNat (if license acceptable) → Wikimedia Commons → eBird
    img_stats = {"inat": 0, "wikimedia": 0, "ebird": 0, "none": 0}

    # 6a: iNat images with acceptable licenses
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

    # 6b: Wikimedia Commons via Wikidata P18 for species still without images
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

    # 6c: eBird images as final fallback (birds only)
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
                entry["image_license"] = ""  # eBird: Macaulay Library terms
                entry["image_source"] = "ebird"
                img_stats["ebird"] += 1

    # Count species still without images
    img_stats["none"] = sum(1 for e in taxonomy.values() if not e["image_url"])
    stats["images"] = img_stats

    # Compute final coverage stats: count unique locales across all species
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


def main():
    parser = argparse.ArgumentParser(
        description="Build unified taxonomy from iNat + AviList")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show stats without writing output")
    args = parser.parse_args()

    cfg = load_config()

    print("Loading sources...")
    inat = load_inat()
    avilist_rows = load_avilist(cfg)
    print(f"  iNaturalist: {len(inat)} species")
    print(f"  AviList:     {len(avilist_rows)} bird species")

    print("\nBuilding taxonomy...")
    taxonomy, stats = build_taxonomy(inat, avilist_rows)

    # Summary
    total_birds = (stats["direct"] + stats["common_name"] + stats["wikidata"]
                   + stats["inat_only"] + stats["avilist_only"])
    matched_birds = stats["direct"] + stats["common_name"] + stats["wikidata"]
    inat_birds = matched_birds + stats["inat_only"]
    print(f"\n  Total species: {len(taxonomy)}")
    print(f"  Birds:         {total_birds}")
    print(f"    Direct match:     {stats['direct']}")
    print(f"    Common name:      {stats['common_name']}")
    print(f"    Wikidata:         {stats['wikidata']}")
    print(f"    iNat only:        {stats['inat_only']} (no eBird code)")
    print(f"    AviList only:     {stats['avilist_only']} (no iNat ID)")
    print(f"    Match rate:       {matched_birds}/{inat_birds} "
          f"iNat birds ({100 * matched_birds / max(1, inat_birds):.1f}%)")
    print(f"  Non-birds:     {stats['non_bird']}")

    # Wikidata identifier stats
    wd_ids = stats.get("wikidata_ids", {})
    wd_cov = stats.get("wikidata_coverage", 0)
    print(f"\n  Wikidata identifiers ({wd_cov}/{len(taxonomy)} species found):")
    for key, count in wd_ids.items():
        print(f"    {key}: {count}")

    # Common name coverage stats
    coverage = stats.get("coverage", {})
    ebird_n = stats.get("ebird_names", {})
    wd_labels = stats.get("wikidata_labels", {})
    total = len(taxonomy)
    n_locales = len(coverage)
    top_n = 30
    print(f"\n  Common name coverage ({total} species, "
          f"{n_locales} locales, top {top_n}):")
    print(f"    {'Locale':<8} {'Total':>7} {'%':>6}  "
          f"{'eBird':>7} {'Wikidata':>8}")
    sorted_locs = sorted(coverage.keys(),
                         key=lambda x: -coverage.get(x, 0))
    for loc in sorted_locs[:top_n]:
        cov = coverage.get(loc, 0)
        eb = ebird_n.get(loc, 0)
        wd = wd_labels.get(loc, 0)
        pct = 100 * cov / max(1, total)
        print(f"    {loc:<8} {cov:>7} {pct:>5.1f}%  {eb:>7} {wd:>8}")
    if n_locales > top_n:
        print(f"    ... and {n_locales - top_n} more locales")

    # Default image stats
    img = stats.get("images", {})
    total_with_img = img.get("inat", 0) + img.get("wikimedia", 0) + img.get("ebird", 0)
    print(f"\n  Default images ({total_with_img}/{total} species):")
    print(f"    iNat (permissive):  {img.get('inat', 0)}")
    print(f"    Wikimedia Commons:  {img.get('wikimedia', 0)}")
    print(f"    eBird:              {img.get('ebird', 0)}")
    print(f"    No image:           {img.get('none', 0)}")

    if args.dry_run:
        # Show some unmatched examples
        unmatched = [(k, v) for k, v in taxonomy.items()
                     if v["taxon_group"] == "Aves" and v["match_source"] == "inat_only"]
        if unmatched:
            unmatched.sort(key=lambda x: -x[1].get("observations_count", 0))
            print(f"\n  Top unmatched iNat birds (by observations):")
            for sci, rec in unmatched[:15]:
                cn = rec.get("preferred_common_name", "")
                obs = rec.get("observations_count", 0)
                print(f"    {sci} — {cn} ({obs:,} obs)")
        return

    # Write output
    RAW.mkdir(exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(taxonomy, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUTPUT_FILE)
    size_mb = OUTPUT_FILE.stat().st_size / 1024 / 1024
    print(f"\n  Written: {OUTPUT_FILE} ({size_mb:.1f} MB)")
    print("Done!")


if __name__ == "__main__":
    main()
