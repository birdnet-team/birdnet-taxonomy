#!/usr/bin/env python3
"""
Build the unified species metadata file.

Combines two phases:
  Phase 1 — Taxonomy: Cross-reference iNaturalist, AviList, Wikidata, and
            eBird data to build a canonical species list with common names,
            images, and external identifiers. All data is read from
            pre-collected JSON files — no API calls.
  Phase 2 — Merge: Enrich each species with a single description using the
            priority Claude > Wikipedia > eBird, then write the final output.

Input files (all in raw_data/):
  - inat_data.json         (from collectors/inat.py)
  - ebird_data.json        (from collectors/ebird.py)
  - ebird_names.json       (from collectors/ebird.py --names-only)
  - wikidata_data.json     (from collectors/wikidata.py)
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
import io
import json
import os
import re
import zipfile
from collections import Counter
from pathlib import Path

from urllib.parse import quote
from config import load_config, image_url_prefix
from collectors._common import (
    ROOT, RAW_DIR, ACCEPTABLE_LICENSES, LOCALE_NORMALIZE,
    is_full_species_name, load_json, save_json,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INAT_FILE = RAW_DIR / "inat_data.json"
EBIRD_DATA_FILE = RAW_DIR / "ebird_data.json"
EBIRD_NAMES_FILE = RAW_DIR / "ebird_names.json"
WIKIDATA_FILE = RAW_DIR / "wikidata_data.json"
WIKI_DATA_FILE = RAW_DIR / "wikipedia_data.json"
CLAUDE_DATA_FILE = RAW_DIR / "claude_data.json"
MACAULAY_DATA_FILE = RAW_DIR / "macaulay_data.json"
XC_DATA_FILE = RAW_DIR / "xc_data.json"
TAXONOMY_FILE = RAW_DIR / "taxonomy.json"
MANUAL_OVERRIDES_FILE = ROOT / "overrides" / "species_overrides.csv"
BN_IDS_FILE = ROOT / "bn_ids.json"




# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _parse_image_author(attribution: str, source: str = "") -> str:
    """Extract author name from attribution strings."""
    if not attribution:
        return ""
    text = attribution.strip()

    if source == "ebird" or (" - " in text and not text.startswith("(")):
        text = text.split(" - ", 1)[-1].strip()
        text = re.sub(r"\s*/\s*Macaulay Library.*", "", text, flags=re.IGNORECASE)

    elif source == "inat" or text.startswith("(c)"):
        text = re.sub(r"^\(c\)\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\(CC[^)]*\)", "", text)
        # "uploaded by X" → keep only X
        m = re.search(r",?\s*uploaded\s+by\s+(.+)", text, flags=re.IGNORECASE)
        if m:
            text = m.group(1)
        # Strip rights / copyright boilerplate
        text = re.sub(r",?\s*(no|some|all)\s+(known\s+)?(copy)?rights?\s+"
                       r"(reserved|restrictions).*", "", text,
                       flags=re.IGNORECASE)

    # Sanitise: strip bracketed content, emoji, and non-name characters
    text = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", text)   # (...) and [...]
    text = re.sub(                                         # emoji & symbols
        r"[\U0001F000-\U0001FFFF"
        r"\U00002600-\U000027BF"
        r"\U0000FE00-\U0000FE0F"
        r"\U0000200D"
        r"\U000E0020-\U000E007F]+", "", text)
    text = re.sub(r"[<>{}|\\^~`/]", "", text)             # stray markup/path chars
    text = re.sub(r"\s+", " ", text)                       # collapse whitespace

    return text.strip(" ,.") if text.strip(" ,.") else ""



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


def load_manual_overrides() -> dict[str, dict]:
    """Load repo-tracked manual species overrides from CSV."""
    if not MANUAL_OVERRIDES_FILE.exists():
        return {}

    overrides: dict[str, dict] = {}
    with open(MANUAL_OVERRIDES_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for line_no, row in enumerate(reader, start=2):
            sci = (row.get("scientific_name") or "").strip()
            if not sci:
                raise ValueError(
                    f"{MANUAL_OVERRIDES_FILE}: line {line_no}: scientific_name is required"
                )
            if sci in overrides:
                raise ValueError(
                    f"{MANUAL_OVERRIDES_FILE}: line {line_no}: duplicate scientific_name '{sci}'"
                )

            image_fields = {
                "image_url": (row.get("image_url") or "").strip(),
                "image_author": (row.get("image_author") or "").strip(),
                "image_license": (row.get("image_license") or "").strip(),
                "image_source": (row.get("image_source") or "").strip(),
            }
            has_any_image = any(image_fields.values())
            has_all_image = all(image_fields.values())
            if has_any_image and not has_all_image:
                raise ValueError(
                    f"{MANUAL_OVERRIDES_FILE}: line {line_no}: image override requires image_url, image_author, image_license, and image_source"
                )

            crop_anchor_raw = (row.get("image_crop_anchor") or "").strip()
            crop_anchor = None
            if crop_anchor_raw:
                try:
                    crop_anchor = int(crop_anchor_raw)
                except ValueError as exc:
                    raise ValueError(
                        f"{MANUAL_OVERRIDES_FILE}: line {line_no}: invalid image_crop_anchor '{crop_anchor_raw}'"
                    ) from exc
                if crop_anchor < 1 or crop_anchor > 9:
                    raise ValueError(
                        f"{MANUAL_OVERRIDES_FILE}: line {line_no}: image_crop_anchor must be 1-9"
                    )

            overrides[sci] = {
                **image_fields,
                "image_crop_anchor": crop_anchor,
                "source_url": (row.get("source_url") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
            }
    return overrides


def print_manual_override_stats(overrides: dict[str, dict]):
    """Print all active manual overrides in a concise, human-readable form."""
    if not overrides:
        return

    print("\n  Active manual overrides:")
    for sci_name in sorted(overrides):
        override = overrides[sci_name]
        parts: list[str] = []
        if override.get("image_url"):
            parts.append("image")
        if override.get("image_crop_anchor") is not None:
            parts.append(f"crop={override['image_crop_anchor']}")
        if override.get("source_url"):
            parts.append(f"source={override['source_url']}")
        if override.get("notes"):
            parts.append(f"notes={override['notes']}")
        detail = ", ".join(parts) if parts else "no-op"
        print(f"    {sci_name}: {detail}")


# ---------------------------------------------------------------------------
# Phase 1: Build taxonomy
# ---------------------------------------------------------------------------

def build_taxonomy(inat: dict, avilist_rows: list[dict]) -> tuple[dict, dict]:
    """Cross-reference iNat, AviList, Wikidata, and eBird to build a taxonomy.

    All data is read from pre-collected JSON files — no API calls.
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

    # Load pre-collected Wikidata and eBird names
    wikidata = load_json(WIKIDATA_FILE)
    ebird_names = load_json(EBIRD_NAMES_FILE)
    if wikidata:
        print(f"  Wikidata:    {len(wikidata)} species")
    if ebird_names:
        print(f"  eBird names: {len(ebird_names)} species codes")

    matched_sci = set()
    stats = {"direct": 0, "common_name": 0, "wikidata": 0, "inat_only": 0,
             "avilist_only": 0, "non_bird": 0, "excluded_non_species": 0}

    # Pass 1: Process all iNat species
    for sci_name, rec in inat.items():
        if rec.get("inat_id") is None:
            continue
        if not is_full_species_name(sci_name):
            stats["excluded_non_species"] += 1
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
                    # Try Wikidata for eBird code
                    wd_code = wikidata.get(sci_name, {}).get("ebird_code", "")
                    if wd_code:
                        ebird_code = wd_code
                        match_source = "wikidata"
                        stats["wikidata"] += 1
                    else:
                        match_source = "inat_only"
                        stats["inat_only"] += 1
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
            "ml_taxon_code": "",
            "xc_name": "",
            "image_url": "",
            "image_author": "",
            "image_license": "",
            "image_source": "",
            "match_source": match_source,
        }

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
            "ml_taxon_code": "",
            "xc_name": "",
            "image_url": "",
            "image_author": "",
            "image_license": "",
            "image_source": "",
            "match_source": "avilist_only",
        }
        stats["avilist_only"] += 1

    # Pass 3: Wikidata identifiers (GBIF, NCBI, etc.)
    id_keys = ("gbif_id", "ncbi_id", "avibase_id", "birdlife_id")
    id_counts = {k: 0 for k in id_keys}
    wd_coverage = 0
    for sci in taxonomy:
        wd = wikidata.get(sci, {})
        if not wd:
            continue
        wd_coverage += 1
        for key in id_keys:
            val = wd.get(key, "")
            if val:
                taxonomy[sci][key] = val
                id_counts[key] += 1
    stats["wikidata_ids"] = id_counts
    stats["wikidata_coverage"] = wd_coverage

    # Pass 3b: Macaulay Library + Xeno-Canto codes
    macaulay = load_json(MACAULAY_DATA_FILE)
    xc_data = load_json(XC_DATA_FILE)
    ml_count = 0
    xc_count = 0
    for sci in taxonomy:
        ml_code = macaulay.get(sci, {}).get("ml_taxon_code", "")
        if ml_code:
            taxonomy[sci]["ml_taxon_code"] = ml_code
            ml_count += 1
        xc_name = xc_data.get(sci, {}).get("xc_name", "")
        if xc_name:
            taxonomy[sci]["xc_name"] = xc_name
            xc_count += 1
    stats["ml_taxon_codes"] = ml_count
    stats["xc_names"] = xc_count

    # Pass 4: eBird common names (authority for bird names)
    ebird_name_counts: dict[str, int] = {}
    if ebird_names:
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
    wd_label_counts: dict[str, int] = {}
    for sci in taxonomy:
        wd = wikidata.get(sci, {})
        labels = wd.get("labels", {})
        for loc, label in labels.items():
            if loc not in taxonomy[sci]["common_names"]:
                taxonomy[sci]["common_names"][loc] = label
                wd_label_counts[loc] = wd_label_counts.get(loc, 0) + 1
    stats["wikidata_labels"] = wd_label_counts

    # Pass 6: Default image selection
    # Priority: iNat taxon (licensed) → eBird → Wikimedia → iNat observation
    img_stats = {"inat": 0, "ebird": 0, "wikimedia": 0, "inat_obs": 0,
                 "none": 0}

    print("\n  Selecting default images...")

    # 6a: iNat taxon photo — only if CC-licensed
    for sci, entry in taxonomy.items():
        inat_rec = inat.get(sci, {})
        url = inat_rec.get("image_url", "")
        lic = inat_rec.get("image_license", "")
        if url and lic in ACCEPTABLE_LICENSES:
            entry["image_url"] = url
            entry["image_author"] = _parse_image_author(
                inat_rec.get("image_attribution", ""), "inat")
            entry["image_license"] = lic
            entry["image_source"] = "iNaturalist"
            img_stats["inat"] += 1

    # 6b: eBird / Macaulay Library
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
                asset_id = eb["url"].rstrip("/").rsplit("/", 1)[-1]
                entry["image_source"] = f"Macaulay Library ML{asset_id}"
                img_stats["ebird"] += 1

    # 6c: Wikimedia Commons (from wikidata_data.json)
    for sci, entry in taxonomy.items():
        if entry["image_url"]:
            continue
        wd = wikidata.get(sci, {})
        wd_img = wd.get("image")
        if wd_img and wd_img.get("url"):
            entry["image_url"] = wd_img["url"]
            entry["image_author"] = wd_img.get("attribution", "")
            entry["image_license"] = wd_img.get("license", "")
            entry["image_source"] = "Wikimedia"
            img_stats["wikimedia"] += 1

    # 6d: iNat observation photo (CC-licensed fallback from observations API)
    for sci, entry in taxonomy.items():
        if entry["image_url"]:
            continue
        inat_rec = inat.get(sci, {})
        obs_photo = inat_rec.get("obs_photo")
        if obs_photo and obs_photo.get("url"):
            entry["image_url"] = obs_photo["url"]
            entry["image_author"] = _parse_image_author(
                obs_photo.get("attribution", ""), "inat")
            entry["image_license"] = obs_photo.get("license", "")
            entry["image_source"] = "iNaturalist"
            img_stats["inat_obs"] += 1

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
    if stats.get("excluded_non_species"):
        print(f"  Excluded:      {stats['excluded_non_species']} non-species iNat taxa")

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
    total_with_img = (img.get("inat", 0) + img.get("ebird", 0)
                      + img.get("wikimedia", 0) + img.get("inat_obs", 0))
    print(f"\n  Default images ({total_with_img}/{total} species):")
    print(f"    iNat (taxon photo):    {img.get('inat', 0)}")
    print(f"    eBird:                 {img.get('ebird', 0)}")
    print(f"    Wikimedia Commons:     {img.get('wikimedia', 0)}")
    print(f"    iNat (observation):    {img.get('inat_obs', 0)}")
    print(f"    No image:              {img.get('none', 0)}")


# ---------------------------------------------------------------------------
# Phase 2: Merge into final metadata
# ---------------------------------------------------------------------------

def _load_bn_ids() -> dict[str, int]:
    """Load persistent BirdNET species ID mapping."""
    if BN_IDS_FILE.exists():
        with open(BN_IDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_bn_ids(bn_ids: dict[str, int]) -> None:
    """Save BirdNET species ID mapping (atomic write)."""
    tmp = BN_IDS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bn_ids, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BN_IDS_FILE)


def build_metadata(taxonomy: dict, ebird: dict, wiki: dict,
                   claude: dict = None,
                   manual_overrides: dict | None = None,
                   reassign_ids: bool = False) -> list[dict]:
    """Merge taxonomy + raw sources into final species records.

    Each record:
      scientific_name, common_name, taxon_group, common_names,
      descriptions, description_source, wikipedia_urls,
            image, image_author, image_license, image_source,
      inat_id, ebird_code, gbif_id, ncbi_id, avibase_id, birdlife_id,
      observations_count
    """
    records = []
    desc_sources: dict[str, int] = Counter()

    if claude is None:
        claude = {}
    if manual_overrides is None:
        manual_overrides = {}

    image_prefix = image_url_prefix()

    for sci_name, tax in taxonomy.items():
        if not is_full_species_name(sci_name):
            continue
        wp = wiki.get(sci_name, {})
        eb = ebird.get(sci_name, {})
        cl = claude.get(sci_name, {})

        # Descriptions: locale → text.
        # Base layer: Wikipedia (multi-locale) or eBird (English-only).
        # Claude overlay: replaces only the locales it provides.
        descriptions: dict[str, str] = {}
        description_source = ""
        wikipedia_urls: dict[str, str] = {}

        # Wikipedia: use English extract, plus any locale extracts and URLs
        wp_extracts = wp.get("extracts", {})
        if wp.get("extract"):
            descriptions["en"] = wp["extract"]
            description_source = "wikipedia"
        elif wp_extracts:
            # No English extract but locale extracts exist
            description_source = "wikipedia"

        if description_source == "wikipedia":
            for loc, text in wp_extracts.items():
                if loc != "en" and text:
                    descriptions[loc] = text
            for loc, url in wp.get("wikipedia_urls", {}).items():
                if url:
                    wikipedia_urls[loc] = url

        if not description_source:
            if eb.get("description"):
                descriptions["en"] = eb["description"]
                description_source = "ebird"

        # Claude overlay — replace only the locales Claude provides
        claude_locales: list[str] = []
        cl_extracts = cl.get("extracts", {})
        for loc, text in cl_extracts.items():
            if text:
                descriptions[loc] = text
                claude_locales.append(loc)

        if descriptions.get("en") and "en" in cl_extracts and cl_extracts["en"]:
            description_source = "claude"

        desc_sources[description_source or "none"] += 1
        override = manual_overrides.get(sci_name, {})
        image_url = override.get("image_url") or tax.get("image_url", "")
        image_author = override.get("image_author") or tax.get("image_author", "")
        image_license = override.get("image_license") or tax.get("image_license", "")
        image_source = override.get("image_source") or tax.get("image_source", "")
        image = None
        if image_url:
            encoded_sci = quote(sci_name, safe='')
            image = {
                "src": image_url,
                "thumb": f"{image_prefix}/api/image/{encoded_sci}?size=thumb",
                "medium": f"{image_prefix}/api/image/{encoded_sci}?size=medium",
            }

        record = {
            "birdnet_id": None,
            "scientific_name": sci_name,
            "common_name": tax.get("preferred_common_name", ""),
            "taxon_group": tax.get("taxon_group", ""),
            "common_names": tax.get("common_names", {}),
            "descriptions": descriptions,
            "description_source": description_source,
            "claude_locales": sorted(claude_locales),
            "wikipedia_urls": wikipedia_urls,
            "image": image,
            "image_author": image_author,
            "image_license": image_license,
            "image_source": image_source,
            "image_crop_anchor": override.get("image_crop_anchor"),
            "inat_id": tax.get("inat_id"),
            "ebird_code": tax.get("ebird_code", ""),
            "gbif_id": tax.get("gbif_id", ""),
            "ncbi_id": tax.get("ncbi_id", ""),
            "avibase_id": tax.get("avibase_id", ""),
            "birdlife_id": tax.get("birdlife_id", ""),
            "ml_taxon_code": tax.get("ml_taxon_code", ""),
            "xc_name": tax.get("xc_name", ""),
            "observations_count": tax.get("observations_count", 0),
        }
        records.append(record)

    # Assign BirdNET species IDs (persistent, append-only)
    if reassign_ids:
        # Re-sort and reassign all IDs from scratch (pre-release only)
        group_order_bn = {"Aves": 0, "Mammalia": 1, "Reptilia": 2,
                          "Amphibia": 3, "Insecta": 4}
        sorted_names = sorted(
            [r["scientific_name"] for r in records],
            key=lambda s: (
                group_order_bn.get(
                    next((r["taxon_group"] for r in records
                          if r["scientific_name"] == s), ""), 99),
                s,
            ),
        )
        bn_ids = {name: i for i, name in enumerate(sorted_names, start=1)}
        _save_bn_ids(bn_ids)
        for rec in records:
            rec["birdnet_id"] = f"BN{bn_ids[rec['scientific_name']]:05d}"
        print(f"  BirdNET IDs: reassigned {len(bn_ids)} IDs")
    else:
        bn_ids = _load_bn_ids()
        next_id = max(bn_ids.values(), default=0) + 1
        new_count = 0
        for rec in records:
            sci = rec["scientific_name"]
            if sci not in bn_ids:
                bn_ids[sci] = next_id
                next_id += 1
                new_count += 1
            rec["birdnet_id"] = f"BN{bn_ids[sci]:05d}"
        if new_count:
            _save_bn_ids(bn_ids)
            print(f"  BirdNET IDs: {new_count} new (total {len(bn_ids)})")
        else:
            print(f"  BirdNET IDs: {len(bn_ids)} (no new)")

    # Sort by BirdNET ID
    records.sort(key=lambda r: r.get("birdnet_id") or "")

    # Print stats
    groups = Counter(r["taxon_group"] for r in records)
    n_locales = len(set(
        loc for r in records for loc in r.get("common_names", {}).keys()
    ))
    img_sources = Counter(
        (r.get("image_source", "") or "none").split(" ML")[0]
        for r in records
    )

    print(f"  {len(records)} species")
    for g, n in sorted(groups.items()):
        print(f"    {g}: {n}")

    print(f"\n  Common names: {n_locales} locales")

    print(f"\n  Descriptions:")
    for src, cnt in desc_sources.most_common():
        print(f"    {src}: {cnt}")

    n_translated = sum(1 for r in records if r.get("descriptions"))
    desc_locales = sorted(set(
        loc for r in records for loc in r.get("descriptions", {}).keys()
    ))
    if n_translated:
        print(f"    translated: {n_translated} species, "
              f"{len(desc_locales)} locales ({', '.join(desc_locales)})")

    print(f"\n  Images:")
    for src, cnt in img_sources.most_common():
        print(f"    {src}: {cnt}")

    return records


def records_to_csv(records: list[dict]) -> str:
    """Convert records to CSV without description excerpts."""
    locale_counts: Counter[str] = Counter()
    for r in records:
        locale_counts.update(r.get("common_names", {}).keys())
    top_locales = [loc for loc, _ in locale_counts.most_common(30)]

    base_cols = [
        "birdnet_id",
        "scientific_name", "common_name", "taxon_group",
        "inat_id", "ebird_code", "gbif_id", "ncbi_id",
        "avibase_id", "birdlife_id", "ml_taxon_code", "xc_name",
        "observations_count",
        "description_source",
        "image_url",
        "image_author", "image_license", "image_source",
    ]
    locale_cols = [f"common_name_{loc}" for loc in top_locales]
    fieldnames = base_cols + locale_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for rec in records:
        row = {k: rec.get(k, "") for k in base_cols}
        row["image_url"] = (rec.get("image") or {}).get("medium", "")
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
    parser.add_argument("--reassign-ids", action="store_true",
                        help="Regenerate all BirdNET IDs from scratch "
                             "(pre-release only, breaks existing IDs)")
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
    manual_overrides = load_manual_overrides()
    unknown_overrides = sorted(set(manual_overrides) - set(taxonomy))
    if unknown_overrides:
        joined = ", ".join(unknown_overrides[:10])
        if len(unknown_overrides) > 10:
            joined += f", ... (+{len(unknown_overrides) - 10} more)"
        raise ValueError(
            f"Manual overrides reference unknown species: {joined}"
        )
    print(f"  Overrides:  {len(manual_overrides):>8} species")
    print_manual_override_stats(manual_overrides)

    print("\nMerging...")
    records = build_metadata(taxonomy, ebird, wiki, claude, manual_overrides,
                              reassign_ids=args.reassign_ids)

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
