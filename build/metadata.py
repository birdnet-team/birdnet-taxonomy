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
  - translate_data.json    (from collectors/translate.py — optional)
  - avicommons_data.json   (from collectors/avicommons.py — optional)
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
import difflib
import html
import io
import json
import os
import re
import zipfile
from collections import Counter
from pathlib import Path

from urllib.parse import quote, unquote
from config import load_config, image_url_prefix, LOCALE_NAMES
from collectors._common import (
    ROOT, RAW_DIR, ACCEPTABLE_LICENSES, LOCALE_NORMALIZE, TRUSTED_XC_METHODS,
    clean_aliases, is_clean_common_name, is_clean_scientific_name,
    is_full_species_name, load_json, load_manual_species_aliases,
    related_species_names, same_epithet_stem, save_json, xc_record_method,
)
from utils.audit_metadata import audit_records, has_failures, print_report
from utils.description_quality import (
    clean_description_text, describes_species, is_disambiguation_text, names_other_species,
    word_count,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INAT_FILE = RAW_DIR / "inat_data.json"
EBIRD_DATA_FILE = RAW_DIR / "ebird_data.json"
EBIRD_NAMES_FILE = RAW_DIR / "ebird_names.json"
EBIRD_TAXONOMY_FILE = RAW_DIR / "ebird_taxonomy.json"
WIKIDATA_FILE = RAW_DIR / "wikidata_data.json"
WIKI_DATA_FILE = RAW_DIR / "wikipedia_data.json"
TRANSLATE_DATA_FILE = RAW_DIR / "translate_data.json"
MACAULAY_DATA_FILE = RAW_DIR / "macaulay_data.json"
XC_DATA_FILE = RAW_DIR / "xc_data.json"
OBSERVATIONORG_DATA_FILE = RAW_DIR / "observationorg_data.json"
AVICOMMONS_FILE = RAW_DIR / "avicommons_data.json"
SOUND_CLASSES_FILE = RAW_DIR / "sound_classes.json"
TAXONOMY_FILE = RAW_DIR / "taxonomy.json"
MANUAL_OVERRIDES_FILE = ROOT / "overrides" / "species_overrides.csv"
MANUAL_ALIASES_FILE = ROOT / "overrides" / "species_aliases.csv"
MANUAL_COMMON_ALIASES_FILE = ROOT / "overrides" / "common_name_aliases.csv"
MANUAL_SPLITS_FILE = ROOT / "overrides" / "bird_splits.csv"
BN_IDS_FILE = ROOT / "bn_ids.json"

ID_FIELDS = (
    "inat_id", "ebird_code", "gbif_id", "ncbi_id", "avibase_id",
    "birdlife_id", "ml_taxon_code", "xc_name", "observationorg_id",
)
EXTERNAL_ID_FIELDS = (
    "ebird_code", "gbif_id", "ncbi_id", "avibase_id", "birdlife_id",
    "ml_taxon_code", "xc_name", "observationorg_id",
)
WIKIDATA_ID_FIELDS = ("gbif_id", "ncbi_id", "avibase_id", "birdlife_id", "wikidata_qid")

# og:description of eBird pages without a species account (generic site text).
EBIRD_BOILERPLATE = re.compile(r"^\s*(Learn about\b|eBird transforms\b)")

# AviList extinction statuses that exclude a species. Possibly extinct and
# extinct-in-the-wild species stay: they can still be recorded.
AVILIST_EXTINCT = {"extinct", "(extinct)"}

# How an iNat bird was resolved to its AviList species; lower wins when
# several iNat taxa resolve to the same species.
BIRD_MATCH_RANK = {"direct": 0, "common_name": 1, "wikidata": 2, "ebird": 3,
                   "subspecies": 4}

# Localized-name source priority for resolving names shared between species.
NAME_SOURCE_RANK = {"avilist": 0, "ebird": 1, "inat": 2, "wikidata": 3}
NAME_SOURCE_BY_RANK = {rank: src for src, rank in NAME_SOURCE_RANK.items()}

# Locales written in a non-Latin script (Serbian is excluded: it uses both).
NON_LATIN_LOCALES = {
    "am", "ar", "arz", "as", "be", "bg", "bn", "bo", "ckb", "dv", "el", "fa", "gu",
    "he", "hi", "hy", "ja", "ka", "kk", "km", "kn", "ko", "lo", "mk", "ml", "mn",
    "mr", "my", "ne", "or", "pa", "ps", "ru", "sd", "si", "ta", "te", "th", "ti",
    "uk", "ur", "yi", "zh",
}
_LATIN_ONLY_RE = re.compile(r"[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F '’.\-]*")

# Taxon groups Xeno-Canto can filter by; other groups only accept exact or
# reviewed-alias XC names.
XC_FUZZY_GROUPS = {"Aves", "Mammalia", "Insecta", "Amphibia"}
# Groups where an epithet-only XC match (unique within the XC group) is
# credible; insect epithets collide across orders XC doesn't cover.
XC_EPITHET_GROUPS = {"Aves", "Mammalia", "Amphibia"}




# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _parse_image_author(attribution: str, source: str = "") -> str:
    """Extract author name from attribution strings."""
    if not attribution:
        return ""
    text = html.unescape(attribution).strip()

    if source == "ebird" or (" - " in text and not text.startswith("(")):
        text = text.split(" - ", 1)[-1].strip()
        text = re.sub(r"\s*/\s*Macaulay Library.*", "", text, flags=re.IGNORECASE)

    elif source == "inat" or text.startswith("(c)"):
        text = re.sub(r"^\(c\)\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\(CC[^)]*\)", "", text)
        # Strip full-width parenthesised CC tags (Chinese)
        text = re.sub(r"（CC[^）]*）", "", text)
        # "uploaded by X" → keep only X  (+ multilingual variants)
        m = re.search(
            r",?\s*(?:uploaded\s+by|uploadet\s+af|subido\s+por"
            r"|carregado\s+por|загрузил|lähettänyt|feltöltötte:"
            r"|によって投稿されました"
            r"|由\s*.+?\s*上传"
            r")\s*(.+)",
            text, flags=re.IGNORECASE)
        if m:
            text = m.group(1)
        # Strip rights / copyright boilerplate (multilingual)
        text = re.sub(
            r",?\s*(?:"
            r"(?:no|some|all)\s+(?:known\s+)?(?:copy)?rights?\s+(?:reserved|restrictions)"
            r"|alcuni\s+diritti\s+riservati"
            r"|certains\s+droits\s+réservés"
            r"|kai\s+kurios\s+teisės\s+saugomos"
            r"|visos\s+teisės\s+saugomos"
            r"|nogle\s+rettigheder\s+forbeholdes"
            r"|некоторые\s+права\s+защищены"
            r"|все\s+права\s+защищены"
            r"|нет\s+известных\s+ограничений\s+авторского\s+права.*"
            r"|nėra\s+žinomų\s+autorinių\s+teisių\s+apribojimų.*"
            r"|保留部分权利.*"
            r"|لا توجد قيود معروفة لحقوق الطبع والنشر.*"
            r"|no\s+known\s+copyright\s+restrictions.*"
            r").*", "", text, flags=re.IGNORECASE)

    # Rights boilerplate that also appears in Commons artist fields
    text = re.sub(r"[،,]?\s*لا توجد قيود معروفة لحقوق الطبع والنشر.*", "", text)

    # Sanitise: strip bracketed content, emoji, and non-name characters
    text = re.sub(r"^\((.+)\)$", r"\1", text.strip())      # "(Hans Hillewaert)"
    text = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", text)   # (...) and [...]
    text = re.sub(                                         # emoji & symbols
        r"[\U0001F000-\U0001FFFF"
        r"\U00002600-\U000027BF"
        r"\U0000FE00-\U0000FE0F"
        r"\U0000200D"
        r"\U000E0020-\U000E007F]+", "", text)
    text = re.sub(r"[<>{}|\\^~`/]", "", text)             # stray markup/path chars
    text = re.sub(r"\s+", " ", text)                       # collapse whitespace

    return text.strip(" ,.\u3001\uff0c\u060c") if text.strip(" ,.\u3001\uff0c\u060c") else ""



def _attributable(author: str, license_name: str) -> bool:
    """An image can be credited: it has an author, or its license (public
    domain, CC0) needs none."""
    return bool(author) or license_name == "pd" or license_name.startswith("cc0")


# Commons files that show a skull or a postage stamp rather than the animal.
_NOT_ANIMAL_IMAGE_RE = re.compile(r"\b(?:skull|skeleton|cranium|stamp)\b", re.IGNORECASE)


def _file_names_other_species(url: str, sci: str, own: set[str], species: set[str]) -> bool:
    """True if a Commons file name names another species of the taxonomy
    ("Lesser_moorhen_(Paragallinula_angulata).jpg" as P18 of Rallicula
    mayri). A trinomial ending in the species' epithet ("Pseudacris
    streckeri illinoensis") is the species itself, an old subspecies name."""
    filename = unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
    for m in re.finditer(r"\b([A-Z][a-z]+) ([a-z]{3,})(?: ([a-z]{3,}))?", filename):
        binomial = f"{m.group(1)} {m.group(2)}"
        if (binomial in species and binomial not in own
                and not (m.group(3) and same_epithet_stem(m.group(3), sci.split()[-1]))):
            return True
    return False


def _usable_wikimedia_image(wd_rec: dict) -> dict | None:
    """A record's Commons image, unless it is a vector file (drawings, maps),
    a skull or stamp, or its license requires attribution and no author is
    known."""
    img = wd_rec.get("image") or {}
    url = img.get("url", "")
    filename = unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
    if not url or url.lower().endswith(".svg") or _NOT_ANIMAL_IMAGE_RE.search(filename):
        return None
    author = _parse_image_author(img.get("attribution", ""), "wikimedia")
    return img if _attributable(author, _normalize_license(img.get("license", ""))) else None


def _normalize_license(value: str) -> str:
    """Write Wikimedia license names in the iNat style ("CC BY-SA 4.0" → "cc-by-sa-4.0")."""
    text = (value or "").strip()
    low = text.lower()
    if low in ("public domain", "pd", "no restrictions"):
        return "pd"
    if low.startswith("cc") or low.startswith("gfdl"):
        return re.sub(r"[\s_]+", "-", low)
    return text


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
        # Pages without a photo carry a placeholder URL with no asset ID.
        if code and url and re.search(r"/asset/\d+", url):
            result[code] = {"url": url, "attribution": attr}
    return result


def load_avicommons(cfg: dict) -> dict[str, dict]:
    """Load Avicommons photos → {ebird_code: {sci, name, src, thumb, medium, author, license}}.

    A photo listed under several codes (e.g. both halves of a split) shows
    only one of them, so it is dropped. CC0 has a single version; Avicommons
    labels some "CC0 2.0" etc., which are written as plain "cc0".
    """
    photos = load_json(AVICOMMONS_FILE).get("photos", [])
    av_cfg = cfg.get("avicommons", {})
    base = av_cfg.get("static_url", "https://static.avicommons.org").rstrip("/")
    sizes = {"src": 900, "thumb": 160, "medium": 480, **(av_cfg.get("sizes") or {})}
    key_counts = Counter(p.get("key") for p in photos)
    result: dict[str, dict] = {}
    for p in photos:
        code, key = (p.get("code") or "").strip(), (p.get("key") or "").strip()
        if not code or not key or key_counts[key] > 1:
            continue
        author = re.sub(r"^\(c\)\s*", "", " ".join((p.get("by") or "").split()),
                        flags=re.IGNORECASE)
        lic = _normalize_license(p.get("license", ""))
        if lic.startswith("cc0"):
            lic = "cc0"
        if not _attributable(author, lic):
            continue
        result[code] = {
            "sci": (p.get("sciName") or "").strip(),
            "name": (p.get("name") or "").strip(),
            **{size: f"{base}/{code}-{key}-{px}.jpg" for size, px in sizes.items()},
            "author": author,
            "license": lic,
        }
    return result


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------

def load_inat() -> dict:
    if not INAT_FILE.exists():
        print(f"ERROR: {INAT_FILE.name} not found. Run: python -m collectors.inat")
        raise SystemExit(1)
    return load_json(INAT_FILE)


def load_avilist(cfg: dict) -> tuple[list[dict], dict[tuple[str, str], set[str]]]:
    """Load AviList species rows and a (genus, subspecies epithet) → species index."""
    csv_name = cfg.get("avilist", {}).get("csv_file", "")
    csv_path = RAW_DIR / csv_name if csv_name else None
    if not csv_path or not csv_path.exists():
        print(f"WARNING: AviList CSV not found at {csv_path}. "
              "Birds will not have eBird codes. Run: python -m collectors.avilist")
        return [], {}

    rows = []
    subspecies: dict[tuple[str, str], set[str]] = {}
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        # AviList versions the Clements column name (e.g. _v2024, _v2025);
        # resolve it by prefix so a new release doesn't silently drop alt names.
        clements_col = next(
            (c for c in (reader.fieldnames or []) if c.startswith("English_name_Clements")),
            "English_name_Clements_v2025",
        )
        for row in reader:
            rank = row.get("Taxon_rank")
            sci = (row.get("Scientific_name") or "").strip()
            if rank == "subspecies":
                parts = sci.split()
                if len(parts) == 3:
                    subspecies.setdefault((parts[0], parts[2]), set()).add(
                        f"{parts[0]} {parts[1]}"
                    )
                continue
            if rank != "species":
                continue
            code = (row.get("Species_code_Cornell_Lab") or "").strip()
            en_clements = (row.get(clements_col) or "").strip()
            en_avilist = (row.get("English_name_AviList") or "").strip()
            status = (row.get("Extinct_or_possibly_extinct") or "").strip().lower()
            # Keep species even without an eBird code: AviList is the naming
            # authority for birds; a missing code just means no eBird linkage.
            if sci:
                rows.append({
                    "scientific_name": sci,
                    "ebird_code": code,
                    "common_name_clements": en_clements,
                    "common_name_avilist": en_avilist,
                    "extinct": status in AVILIST_EXTINCT,
                    "aliases": [],
                })
    return rows, subspecies


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


def load_manual_aliases() -> dict[str, list[str]]:
    """Load repo-tracked manual scientific-name aliases from CSV."""
    if not MANUAL_ALIASES_FILE.exists():
        return {}

    aliases: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    with open(MANUAL_ALIASES_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"scientific_name", "alias"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{MANUAL_ALIASES_FILE}: missing required columns: {', '.join(sorted(missing))}"
            )
        for line_no, row in enumerate(reader, start=2):
            sci = (row.get("scientific_name") or "").strip()
            alias = (row.get("alias") or "").strip()
            if not is_clean_scientific_name(sci):
                raise ValueError(
                    f"{MANUAL_ALIASES_FILE}: line {line_no}: invalid scientific_name '{sci}'"
                )
            if not is_clean_scientific_name(alias):
                raise ValueError(
                    f"{MANUAL_ALIASES_FILE}: line {line_no}: invalid alias '{alias}'"
                )
            if sci == alias:
                raise ValueError(
                    f"{MANUAL_ALIASES_FILE}: line {line_no}: alias equals scientific_name"
                )
            key = (sci, alias)
            if key in seen:
                raise ValueError(
                    f"{MANUAL_ALIASES_FILE}: line {line_no}: duplicate alias '{alias}' for '{sci}'"
                )
            seen.add(key)
            aliases.setdefault(sci, []).append(alias)
    return aliases


def load_manual_common_aliases() -> dict[str, list[str]]:
    """Load repo-tracked manual common-name search aliases from CSV."""
    if not MANUAL_COMMON_ALIASES_FILE.exists():
        return {}

    aliases: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    with open(MANUAL_COMMON_ALIASES_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"scientific_name", "alias"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{MANUAL_COMMON_ALIASES_FILE}: missing required columns: {', '.join(sorted(missing))}"
            )
        for line_no, row in enumerate(reader, start=2):
            sci = (row.get("scientific_name") or "").strip()
            alias = (row.get("alias") or "").strip()
            if not is_clean_scientific_name(sci):
                raise ValueError(
                    f"{MANUAL_COMMON_ALIASES_FILE}: line {line_no}: invalid scientific_name '{sci}'"
                )
            if not is_clean_common_name(alias):
                raise ValueError(
                    f"{MANUAL_COMMON_ALIASES_FILE}: line {line_no}: invalid alias '{alias}'"
                )
            key = (sci, alias.lower())
            if key in seen:
                raise ValueError(
                    f"{MANUAL_COMMON_ALIASES_FILE}: line {line_no}: duplicate alias '{alias}' for '{sci}'"
                )
            seen.add(key)
            aliases.setdefault(sci, []).append(alias)
    return aliases


def load_bird_splits() -> dict[str, str]:
    """Load reviewed bird splits kept separate from an AviList lump.

    Returns {scientific_name: AviList species it is lumped into}.
    """
    if not MANUAL_SPLITS_FILE.exists():
        return {}
    splits: dict[str, str] = {}
    with open(MANUAL_SPLITS_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"scientific_name", "avilist_species"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{MANUAL_SPLITS_FILE}: missing required columns: {', '.join(sorted(missing))}"
            )
        for line_no, row in enumerate(reader, start=2):
            sci = (row.get("scientific_name") or "").strip()
            parent = (row.get("avilist_species") or "").strip()
            for value in (sci, parent):
                if not is_clean_scientific_name(value):
                    raise ValueError(
                        f"{MANUAL_SPLITS_FILE}: line {line_no}: invalid scientific name '{value}'"
                    )
            if sci == parent or sci in splits:
                raise ValueError(
                    f"{MANUAL_SPLITS_FILE}: line {line_no}: duplicate or self-referencing '{sci}'"
                )
            splits[sci] = parent
    return splits


def is_sound_class_record(record: dict) -> bool:
    return record.get("record_type") == "sound_class"


def is_clean_sound_class_name(name: str) -> bool:
    return is_clean_common_name(name)


def clean_common_aliases(values: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        key = value.lower()
        if not value or key in seen or not is_clean_common_name(value):
            continue
        seen.add(key)
        result.append(value)
    return result


def load_sound_classes() -> dict[str, dict]:
    """Load configured non-species sound classes collected offline.

    Cached classes no longer listed under `sound_classes` in config.yml are
    skipped (the collector keeps them in its cache).
    """
    classes = load_json(SOUND_CLASSES_FILE)
    configured = {str(c.get("name") or "").strip()
                  for c in load_config().get("sound_classes", []) or []}
    result: dict[str, dict] = {}
    for name, rec in classes.items():
        sci = str(rec.get("scientific_name") or name).strip()
        if not is_clean_sound_class_name(sci) or sci not in configured:
            continue
        common_name = str(rec.get("common_name") or sci).strip()
        result[sci] = {
            "inat_id": None,
            "taxon_group": rec.get("taxon_group", ""),
            "iconic_taxon_name": rec.get("taxon_group", ""),
            "preferred_common_name": common_name if is_clean_common_name(common_name) else sci,
            "common_name_alt": "",
            "common_names": {
                LOCALE_NORMALIZE.get(k, k): v
                for k, v in (rec.get("common_names", {}) or {}).items()
                if is_clean_common_name(str(v))
            },
            "common_name_aliases": clean_common_aliases(rec.get("common_name_aliases", [])),
            "observations_count": 0,
            "ebird_code": "",
            "gbif_id": "",
            "ncbi_id": "",
            "avibase_id": "",
            "birdlife_id": "",
            "ml_taxon_code": "",
            "xc_name": "",
            "observationorg_id": "",
            "image_url": rec.get("image_url", ""),
            "image_author": rec.get("image_author", ""),
            "image_license": _normalize_license(rec.get("image_license", "")),
            "image_source": rec.get("image_source", ""),
            "match_source": "sound_class",
            "scientific_name_aliases": clean_common_aliases(
                rec.get("scientific_name_aliases", [])
            ),
            "record_type": "sound_class",
            "wikidata_qid": rec.get("wikidata_qid", ""),
        }
        result[sci]["common_names"].setdefault("en", sci)
    return result


def print_manual_alias_stats(aliases: dict[str, list[str]]):
    """Print active manual aliases in a concise, human-readable form."""
    if not aliases:
        return

    print("\n  Active manual aliases:")
    for sci_name in sorted(aliases):
        print(f"    {sci_name}: {', '.join(aliases[sci_name])}")


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

def _entry_names(sci: str, entry: dict) -> list[str]:
    """Names raw sources may key a species under: canonical first, then source names.

    Collectors key data by the name they were run against (iNat names for
    Wikipedia/translations, older AviList names for eBird pages), which can
    differ from the canonical name after an AviList rename.
    """
    return [sci, *(n for n in entry.get("source_names", []) if n != sci)]


def _raw_lookup(data: dict, names: list[str]) -> dict:
    """Return the first non-empty raw record found under any of the names."""
    for name in names:
        rec = data.get(name)
        if rec:
            return rec
    return {}


def _own_aliases(sci: str, taxonomy: dict, names: list[str]) -> list[str]:
    """Clean aliases, minus this species' own name and other species' names.

    Synonym sources (GBIF, Wikidata) list names that are valid species in
    our taxonomy, e.g. both halves of a split; those can't identify this one.
    """
    return [a for a in clean_aliases(names) if a != sci and a not in taxonomy]


def _new_entry(**fields) -> dict:
    entry = {
        "inat_id": None,
        "inat_key": "",
        "taxon_group": "",
        "iconic_taxon_name": "",
        "preferred_common_name": "",
        "common_name_alt": "",
        "common_names": {},
        "common_name_sources": {},
        "observations_count": 0,
        "ebird_code": "",
        "gbif_id": "",
        "ncbi_id": "",
        "avibase_id": "",
        "birdlife_id": "",
        "ml_taxon_code": "",
        "xc_name": "",
        "observationorg_id": "",
        "image_url": "",
        "image_author": "",
        "image_license": "",
        "image_source": "",
        "match_source": "",
        "source_names": [],
        "scientific_name_aliases": [],
    }
    entry.update(fields)
    return entry


def _inat_common_names(rec: dict) -> dict[str, str]:
    return {
        LOCALE_NORMALIZE.get(k, k): v
        for k, v in (rec.get("common_names", {}) or {}).items()
    }


def _match_avilist_bird(key: str, name: str, rec: dict, avi_by_sci: dict,
                        avi_by_en: dict, avi_by_code: dict,
                        avilist_subspecies: dict, wikidata: dict,
                        ebird_by_sci: dict) -> tuple[str, str]:
    """Resolve an iNat bird to its AviList species. Returns (species, method)."""
    for n in (name, key):
        if n in avi_by_sci:
            return n, "direct"
    common = (rec.get("preferred_common_name") or "").lower()
    rows = [r for r in avi_by_en.get(common, []) if related_species_names(name, r["scientific_name"])]
    if len(rows) == 1:
        return rows[0]["scientific_name"], "common_name"
    for n in (name, key):
        code = wikidata.get(n, {}).get("ebird_code", "")
        if code in avi_by_code:
            return avi_by_code[code]["scientific_name"], "wikidata"
    for n in (name, key):
        for code in ebird_by_sci.get(n, []):
            if code in avi_by_code:
                return avi_by_code[code]["scientific_name"], "ebird"
    # AviList treats the taxon as a subspecies: fold it into the parent species.
    genus, epithet = name.split()[:2]
    parents = avilist_subspecies.get((genus, epithet), set())
    if len(parents) == 1:
        parent = next(iter(parents))
        if parent in avi_by_sci:
            return parent, "subspecies"
    return "", ""


def _assign_ebird_codes(taxonomy: dict, avi_by_sci: dict, ebird_tax: dict,
                        ebird_by_sci: dict, wikidata: dict) -> dict[str, int]:
    """Give each bird at most one eBird code, and each code at most one bird.

    AviList codes are preferred, but only when eBird publishes them (AviList
    can be ahead of eBird). Otherwise fall back to eBird's own scientific
    name, then to Wikidata. AviList leaves the code empty where its species
    concept has no 1:1 eBird match; eBird's same-named species is then the
    closest linkage (names, images) available.
    """
    known = (lambda code: code in ebird_tax) if ebird_tax else (lambda code: bool(code))
    birds = {sci: e for sci, e in taxonomy.items() if e.get("taxon_group") == "Aves"}

    def avilist_code(sci, entry):
        code = avi_by_sci.get(sci, {}).get("ebird_code", "")
        return [code] if known(code) else []

    def ebird_sci_code(sci, entry):
        return ebird_by_sci.get(sci, [])

    def ebird_source_code(sci, entry):
        return [c for n in entry.get("source_names", []) for c in ebird_by_sci.get(n, [])]

    def wikidata_code(sci, entry):
        codes = [wikidata.get(n, {}).get("ebird_code", "") for n in _entry_names(sci, entry)]
        return [c for c in codes if known(c)]

    counts: dict[str, int] = {}
    used: set[str] = set()
    for method, candidates in (("avilist", avilist_code), ("ebird_name", ebird_sci_code),
                               ("ebird_source_name", ebird_source_code),
                               ("wikidata", wikidata_code)):
        wants: dict[str, list[str]] = {}
        for sci, entry in birds.items():
            if entry["ebird_code"]:
                continue
            codes = [c for c in dict.fromkeys(candidates(sci, entry)) if c and c not in used]
            if len(codes) == 1:
                wants.setdefault(codes[0], []).append(sci)
        for code, owners in wants.items():
            if len(owners) == 1:  # a code wanted by several birds is ambiguous
                birds[owners[0]]["ebird_code"] = code
                used.add(code)
                counts[method] = counts.get(method, 0) + 1
    return counts


def _validated_xc_name(sci: str, entry: dict, xc_name: str,
                       name_owners: dict[str, set[str]],
                       genus_groups: dict[str, set[str]], method: str = "",
                       matched_name: str = "") -> str:
    """Reject Xeno-Canto names that belong to a different species.

    The XC collector falls back to epithet, synonym, and English-name
    searches, which can land on an unrelated species sharing the epithet (a
    gecko mapped to an owl, a dragonfly to a bush-cricket). Accept exact names
    and reviewed aliases; otherwise the name must not be another species', its
    genus must fit the taxon group, and the match method must hold up:
    Wikidata's XC ID, an exact English name within the XC group, a synonym
    that is still a trusted alias, or a unique epithet match (birds, mammals,
    amphibians). Untagged legacy names need a related name in those groups
    (or the same genus).
    """
    if not xc_name or not is_clean_scientific_name(xc_name):
        return ""
    trusted = name_owners.get(xc_name) == {sci}
    if xc_name == sci or xc_name in entry.get("scientific_name_aliases", []) or trusted:
        return xc_name
    owners = name_owners.get(xc_name)
    if owners and sci not in owners:
        return ""
    group = entry.get("taxon_group", "")
    if group not in XC_FUZZY_GROUPS:
        return ""
    genus = xc_name.split()[0]
    groups = genus_groups.get(genus)
    if groups and group not in groups:
        return ""
    if method == "synonym" and sci not in name_owners.get(matched_name, set()):
        method = ""  # matched through a name we no longer trust
    if method in TRUSTED_XC_METHODS:
        return xc_name
    related = related_species_names(sci, xc_name)
    if method == "epithet_unique":
        return xc_name if related and group in XC_EPITHET_GROUPS else ""
    if method:
        return ""  # e.g. "epithet": recorded before uniqueness was checked
    if related and (group in XC_EPITHET_GROUPS or genus == sci.split()[0]):
        return xc_name
    return ""


def _resolve_shared_identifiers(taxonomy: dict, sources: dict[str, dict]) -> dict[str, int]:
    """Keep each external identifier on one species only.

    An identifier found via an alias (e.g. a GBIF synonym, or the parent name
    of a recent split) loses to a species whose own name carries it; IDs still
    shared after that are ambiguous and are dropped from all claimants.
    sources maps species → field → the name the ID was matched under.
    """
    dropped: dict[str, int] = {}
    for key in (*WIKIDATA_ID_FIELDS, "ml_taxon_code", "observationorg_id", "xc_name"):
        owners: dict[str, list[str]] = {}
        for sci, entry in taxonomy.items():
            if entry.get(key):
                owners.setdefault(str(entry[key]), []).append(sci)
        for value, claimants in owners.items():
            if len(claimants) < 2:
                continue
            exact = [s for s in claimants if sources.get(s, {}).get(key) in _entry_names(s, taxonomy[s])]
            keep = exact if len(exact) == 1 else []
            for sci in claimants:
                if sci not in keep:
                    taxonomy[sci][key] = ""
                    dropped[key] = dropped.get(key, 0) + 1
    return dropped


def _accept_gbif_keys(taxonomy: dict, wikidata: dict) -> int:
    """Replace GBIF synonym keys with the accepted key of the same taxon.

    Wikidata often carries the key of a GBIF synonym usage (GBIF still lists
    the species under an older genus, or as a subspecies), which returns few
    occurrences. The accepted key is used when its name keeps the species'
    epithet (same taxon) and neither that key nor that species name belongs
    to another species here (a GBIF lump). Returns the number replaced.
    """
    held = Counter(e.get("gbif_id") for e in taxonomy.values() if e.get("gbif_id"))
    replaced = 0
    for sci, entry in taxonomy.items():
        key = entry.get("gbif_id")
        status = next((wikidata[n]["gbif_status"] for n in _entry_names(sci, entry)
                       if (wikidata.get(n, {}).get("gbif_status") or {}).get("key") == key), None)
        if not key or not status or "SYNONYM" not in status.get("status", ""):
            continue
        accepted_key, accepted = status.get("accepted_key", ""), status.get("accepted_name", "")
        words = accepted.split()
        if (not accepted_key or len(words) < 2 or accepted_key in held
                or not same_epithet_stem(words[-1], sci.split()[-1])
                or (len(words) == 2 and accepted != sci and accepted in taxonomy)):
            continue
        entry["gbif_id"] = accepted_key
        held[accepted_key] += 1
        replaced += 1
    return replaced


def _resolve_name_conflicts(taxonomy: dict, extra_scientific: set[str] | None = None) -> int:
    """Drop localized names that a more authoritative source gives another species,
    and names that are just the scientific name.

    Secondary sources (iNat, Wikidata) occasionally carry a neighbouring
    species' name — e.g. a German name that eBird assigns to a different bird.
    For each locale, a name belongs to the species holding it from the best
    source; others fall back to their next candidate or lose the locale.
    """
    # Sources fill untranslated locales with a scientific name (eBird for some
    # es_ES names, Wikidata bot wikis with old combinations); a known
    # scientific name of any species is never a common name.
    scientific = {n.lower() for n in extra_scientific or ()}
    for sci, entry in taxonomy.items():
        scientific.add(sci.lower())
        scientific.update(a.lower() for a in entry.get("scientific_name_aliases", []))
        scientific.update(a.lower() for a in entry.get("xc_aliases", []))

    genera = {n.split()[0] for n in scientific if " " in n}
    # Words that head longer vernacular names in a locale ("Colibri à gorge
    # rubis", "Rana di Lataste") are nouns there, not genera.
    heads: dict[str, Counter] = {}
    for entry in taxonomy.values():
        for loc, cands in entry.get("_name_candidates", {}).items():
            for _, name in cands:
                words = name.lower().split()
                if len(words) >= 3:
                    heads.setdefault(loc, Counter())[words[0]] += 1

    def is_scientific(sci: str, entry: dict, name: str, loc: str = "", rank: int = 0) -> bool:
        clean = " ".join(name.replace("\xa0", " ").split())
        if clean.lower() in scientific:
            return True
        # An unrecorded synonym from iNat or Wikidata: a known genus (or the
        # species' own) with the species' epithet or a gender variant
        # ("Leptodactylus andreae" for Adenomera andreae, "Acanthidops
        # bairdii" for A. bairdi). Curated AviList/eBird names are kept.
        if rank < NAME_SOURCE_RANK["inat"] or not _BINOMIAL_SHAPE_RE.fullmatch(clean):
            return False
        genus, epithet = clean.lower().split()
        if heads.get(loc, Counter())[genus] >= 2:
            return False
        own = [sci] + entry.get("scientific_name_aliases", [])
        return ((genus in genera or genus == sci.split()[0].lower())
                and any(same_epithet_stem(epithet, n.split()[-1]) for n in own))

    def english_copy(entry: dict, loc: str, name: str) -> bool:
        # An untranslated English name in another locale (eBird pt_BR lists
        # them lower-cased); single words ("Kea") are often genuinely shared.
        if loc == "en" or loc.startswith("en-") or len(name.split()) < 2:
            return False
        return name.strip().lower() in entry.get("_english_forms", set())

    def wrong_script(loc: str, name: str) -> bool:
        # A Latin-only name in a locale written in another script is an
        # untranslated English fallback (eBird zh lists many), not a name.
        primary = re.split(r"[-_]", loc)[0]
        return primary in NON_LATIN_LOCALES and bool(_LATIN_ONLY_RE.fullmatch(name.strip()))

    best: dict[tuple[str, str], int] = {}
    for sci, entry in taxonomy.items():
        for loc, cands in entry.get("_name_candidates", {}).items():
            for rank, name in cands:
                if (is_scientific(sci, entry, name, loc, rank) or wrong_script(loc, name)
                        or english_copy(entry, loc, name)):
                    continue
                k = (loc, name.strip().lower())
                best[k] = min(best.get(k, rank), rank)

    dropped = 0
    for sci, entry in taxonomy.items():
        cands_by_loc = entry.pop("_name_candidates", {})
        for loc, cands in cands_by_loc.items():
            chosen = None
            for rank, name in sorted(cands, key=lambda c: c[0]):
                if (is_scientific(sci, entry, name, loc, rank) or wrong_script(loc, name)
                        or english_copy(entry, loc, name)):
                    continue
                if rank <= best[(loc, name.strip().lower())]:
                    chosen = (rank, name)
                    break
            current = entry["common_names"].get(loc)
            if chosen is None:
                if current is not None:
                    entry["common_names"].pop(loc)
                    entry["common_name_sources"].pop(loc, None)
                    dropped += 1
            elif chosen[1] != current:
                entry["common_names"][loc] = chosen[1]
                entry["common_name_sources"][loc] = NAME_SOURCE_BY_RANK[chosen[0]]
                dropped += 1
    return dropped


def build_taxonomy(inat: dict, avilist_rows: list[dict],
                   allowed_groups: set[str] | None = None,
                   avilist_subspecies: dict[tuple[str, str], set[str]] | None = None,
                   bird_splits: dict[str, str] | None = None,
                   ) -> tuple[dict, dict]:
    """Cross-reference iNat, AviList, Wikidata, and eBird to build a taxonomy.

    All data is read from pre-collected JSON files — no API calls.
    If allowed_groups is provided, only species in those taxon groups are
    included (plus AviList-only birds when "Aves" is allowed).
    Returns (taxonomy_dict, stats_dict).
    """
    taxonomy: dict[str, dict] = {}
    avilist_subspecies = avilist_subspecies or {}
    bird_splits = bird_splits or {}

    avi_by_sci = {row["scientific_name"]: row for row in avilist_rows}
    for sci, parent in bird_splits.items():
        if sci in avi_by_sci:
            raise ValueError(f"{MANUAL_SPLITS_FILE}: '{sci}' is already an AviList species")
        if parent not in avi_by_sci or avi_by_sci[parent].get("extinct"):
            raise ValueError(f"{MANUAL_SPLITS_FILE}: '{parent}' is not a living AviList species")
    avi_by_en: dict[str, list[dict]] = {}
    avi_by_code: dict[str, dict] = {}
    for row in avilist_rows:
        if row["ebird_code"]:
            avi_by_code[row["ebird_code"]] = row
        for name in {row["common_name_clements"].lower(), row["common_name_avilist"].lower()}:
            if name:
                avi_by_en.setdefault(name, []).append(row)

    # Load pre-collected Wikidata and eBird names
    wikidata = load_json(WIKIDATA_FILE)
    ebird_names = load_json(EBIRD_NAMES_FILE)
    ebird_tax = load_json(EBIRD_TAXONOMY_FILE).get("species", {})
    ebird_by_sci: dict[str, list[str]] = {}
    for code, rec in ebird_tax.items():
        ebird_by_sci.setdefault(rec.get("scientific_name", ""), []).append(code)
    manual_aliases = load_manual_species_aliases()
    if wikidata:
        print(f"  Wikidata:    {len(wikidata)} species")
    if ebird_names:
        print(f"  eBird names: {len(ebird_names)} species codes")
    if ebird_tax:
        print(f"  eBird taxonomy: {len(ebird_tax)} species")
    else:
        print("  WARNING: ebird_taxonomy.json missing — eBird codes are not validated. "
              "Run: python -m collectors.ebird --names-only")

    stats = {"direct": 0, "common_name": 0, "wikidata": 0, "ebird": 0,
             "subspecies": 0, "folded_birds": 0, "split_override": 0,
             "avilist_only": 0, "non_bird": 0, "excluded_non_species": 0,
             "excluded_extinct": 0, "excluded_bird_no_avilist": 0,
             "excluded_group": 0, "excluded_inactive": 0, "sound_classes": 0}

    # Pass 0: taxa iNat has retired (swaps/merges). Their names become
    # aliases of the replacement taxon instead of duplicate species.
    retired_names: dict[int, list[str]] = {}
    for key, rec in inat.items():
        if rec.get("inat_id") is None or rec.get("is_active", True):
            continue
        stats["excluded_inactive"] += 1
        for tid in rec.get("current_synonymous_taxon_ids") or []:
            retired_names.setdefault(tid, []).extend(
                n for n in (key, rec.get("inat_name")) if n
            )

    # Pass 1: resolve active iNat species to canonical names. Birds resolve to
    # their AviList species (AviList is the bird authority); several iNat
    # taxa can resolve to one AviList species (lumps, subspecies).
    bird_claims: dict[str, list[tuple]] = {}
    split_claims: dict[str, list[tuple]] = {}
    other_claims: dict[str, list[tuple]] = {}
    for key, rec in inat.items():
        if rec.get("inat_id") is None or not rec.get("is_active", True):
            continue
        name = rec.get("inat_name") or key
        if not is_full_species_name(name):
            name = key
        if not is_full_species_name(name):
            stats["excluded_non_species"] += 1
            continue
        if rec.get("extinct"):
            stats["excluded_extinct"] += 1
            continue
        if allowed_groups and rec.get("taxon_group") not in allowed_groups:
            stats["excluded_group"] += 1
            continue
        claim = (-(rec.get("observations_count") or 0), rec["inat_id"], key)
        if rec.get("taxon_group") != "Aves":
            other_claims.setdefault(name, []).append(claim)
            continue
        split = next((n for n in (name, key) if n in bird_splits), "")
        if split:
            # Reviewed exception: kept apart from the AviList species lumping it.
            split_claims.setdefault(split, []).append(claim)
            continue
        target, method = _match_avilist_bird(
            key, name, rec, avi_by_sci, avi_by_en, avi_by_code,
            avilist_subspecies, wikidata, ebird_by_sci,
        )
        if not target:
            stats["excluded_bird_no_avilist"] += 1
            continue
        if avi_by_sci[target].get("extinct"):
            stats["excluded_extinct"] += 1
            continue
        bird_claims.setdefault(target, []).append((BIRD_MATCH_RANK[method], *claim, method))

    def claim_names(key: str) -> list[str]:
        rec = inat[key]
        return list(dict.fromkeys(n for n in (key, rec.get("inat_name")) if n))

    for target, claims in bird_claims.items():
        claims.sort()
        _, _, _, key, method = claims[0]
        rec = inat[key]
        avi_row = avi_by_sci[target]
        stats[method] += 1
        stats["folded_birds"] += len(claims) - 1
        # A subspecies-level iNat taxon is narrower than the AviList species:
        # keep it as an alias, but don't borrow its names or descriptions.
        narrower = method == "subspecies"
        source_names = [] if narrower else [n for n in claim_names(key) if n != target]
        aliases = [n for n in claim_names(key) if n != target]
        for _, _, inat_id, other_key, _ in claims:
            if other_key != key:
                aliases.extend(claim_names(other_key))
            aliases.extend(retired_names.get(inat_id, []))
        avilist_en = avi_row["common_name_avilist"] or avi_row["common_name_clements"]
        clements_en = avi_row["common_name_clements"]
        pref_name = avilist_en or rec.get("preferred_common_name", "")
        common_names = {} if narrower else _inat_common_names(rec)
        if pref_name:
            common_names["en"] = pref_name
        taxonomy[target] = _new_entry(
            inat_id=rec["inat_id"],
            inat_key=key,
            taxon_group="Aves",
            iconic_taxon_name=rec.get("iconic_taxon_name", ""),
            preferred_common_name=pref_name,
            common_name_alt=clements_en if clements_en and clements_en != pref_name else "",
            common_names=common_names,
            common_name_sources={
                loc: "avilist" if loc == "en" and avilist_en else "inat"
                for loc in common_names
            },
            observations_count=rec.get("observations_count", 0),
            match_source=method,
            source_names=source_names,
            scientific_name_aliases=clean_aliases([a for a in aliases if a != target]),
        )

    for sci, claims in split_claims.items():
        claims.sort()
        _, _, key = claims[0]
        rec = inat[key]
        stats["split_override"] += 1
        source_names = [n for n in claim_names(key) if n != sci]
        aliases = list(source_names)
        for _, inat_id, other_key in claims:
            if other_key != key:
                aliases.extend(claim_names(other_key))
            aliases.extend(retired_names.get(inat_id, []))
        # No AviList English name exists for a lumped taxon: use eBird's
        # (Clements) name for the same scientific name, else iNat's.
        ebird_en = next((ebird_tax[c].get("common_name", "") for c in ebird_by_sci.get(sci, [])
                         if ebird_tax[c].get("common_name")), "")
        pref_name = ebird_en or rec.get("preferred_common_name", "")
        common_names = _inat_common_names(rec)
        if pref_name:
            common_names["en"] = pref_name
        taxonomy[sci] = _new_entry(
            inat_id=rec["inat_id"],
            inat_key=key,
            taxon_group="Aves",
            iconic_taxon_name=rec.get("iconic_taxon_name", ""),
            preferred_common_name=pref_name,
            common_names=common_names,
            common_name_sources={
                loc: "ebird" if loc == "en" and ebird_en else "inat" for loc in common_names
            },
            observations_count=rec.get("observations_count", 0),
            match_source="split_override",
            source_names=source_names,
            scientific_name_aliases=clean_aliases([a for a in aliases if a != sci]),
        )
    missing_splits = sorted(set(bird_splits) - set(split_claims))
    if missing_splits:
        print(f"  WARNING: bird splits without an active iNat taxon (skipped): "
              f"{', '.join(missing_splits)}")

    for name, claims in other_claims.items():
        claims.sort()
        _, _, key = claims[0]
        rec = inat[key]
        stats["non_bird"] += 1
        source_names = [n for n in claim_names(key) if n != name]
        aliases = list(source_names)
        for _, inat_id, other_key in claims:
            if other_key != key:
                aliases.extend(claim_names(other_key))
            aliases.extend(retired_names.get(inat_id, []))
        common_names = _inat_common_names(rec)
        taxonomy[name] = _new_entry(
            inat_id=rec["inat_id"],
            inat_key=key,
            taxon_group=rec.get("taxon_group", ""),
            iconic_taxon_name=rec.get("iconic_taxon_name", ""),
            preferred_common_name=rec.get("preferred_common_name", ""),
            common_names=common_names,
            common_name_sources={loc: "inat" for loc in common_names},
            observations_count=rec.get("observations_count", 0),
            match_source="non_bird",
            english_names=rec.get("english_names", []),
            source_names=source_names,
            scientific_name_aliases=clean_aliases([a for a in aliases if a != name]),
        )

    # Pass 2: Add AviList-only species
    for row in avilist_rows:
        sci = row["scientific_name"]
        if sci in taxonomy or row.get("extinct"):
            continue
        en = row["common_name_avilist"] or row["common_name_clements"] or ""
        clements_en = row["common_name_clements"] or ""
        taxonomy[sci] = _new_entry(
            taxon_group="Aves",
            iconic_taxon_name="Aves",
            preferred_common_name=en,
            common_name_alt=clements_en if clements_en and clements_en != en else "",
            common_names={"en": en} if en else {},
            common_name_sources={"en": "avilist"} if en else {},
            match_source="avilist_only",
        )
        stats["avilist_only"] += 1

    # eBird codes (unique, validated against the eBird taxonomy)
    stats["ebird_codes"] = _assign_ebird_codes(
        taxonomy, avi_by_sci, ebird_tax, ebird_by_sci, wikidata,
    )

    # Pass 3: Macaulay Library + observation.org codes (their aliases are
    #         trusted when checking Wikidata IDs and Xeno-Canto names)
    macaulay = load_json(MACAULAY_DATA_FILE)
    obsorg_data = load_json(OBSERVATIONORG_DATA_FILE)
    id_sources: dict[str, dict[str, str]] = {}
    ml_count = 0
    obsorg_count = 0
    for sci, entry in taxonomy.items():
        names = _entry_names(sci, entry)
        # A match made through another species' name (the parent of a recent
        # split, listed as a GBIF synonym) belongs to that species.
        ml = _raw_lookup(macaulay, names)
        if ml.get("matched_name") in taxonomy and ml.get("matched_name") != sci:
            ml = {}
        obs = _raw_lookup(obsorg_data, names)
        if obs.get("matched_name") in taxonomy and obs.get("matched_name") != sci:
            obs = {}
        ml_code = ml.get("ml_taxon_code") or ""
        if entry["taxon_group"] == "Aves":
            # Macaulay identifies birds by their eBird species code; cached ML
            # codes can predate a split, so the validated eBird code wins. A
            # cached code without one is kept only if AviList agrees.
            avi_code = avi_by_sci.get(sci, {}).get("ebird_code", "")
            ml_code = entry["ebird_code"] or (ml_code if ml_code == avi_code else "")
            ml = {**ml, "matched_name": ""} if ml_code else {}
        if ml_code:
            entry["ml_taxon_code"] = ml_code
            id_sources.setdefault(sci, {})["ml_taxon_code"] = ml.get("matched_name") or sci
            ml_count += 1
        if obs.get("observationorg_id") is not None:
            entry["observationorg_id"] = obs["observationorg_id"]
            id_sources.setdefault(sci, {})["observationorg_id"] = obs.get("matched_name") or sci
            obsorg_count += 1
        entry["scientific_name_aliases"] = _own_aliases(sci, taxonomy, [
            *entry["scientific_name_aliases"], *ml.get("aliases", []), *obs.get("aliases", []),
        ])

    # Pass 3b: Wikidata synonym aliases (GBIF synonyms collected per species)
    for sci, entry in taxonomy.items():
        for name in _entry_names(sci, entry):
            wd = wikidata.get(name) or {}
            entry["scientific_name_aliases"] = _own_aliases(
                sci, taxonomy, [*entry["scientific_name_aliases"], *wd.get("aliases", [])]
            )

    # Pass 3c: Xeno-Canto names, validated against every other species' names
    xc_data = load_json(XC_DATA_FILE)
    name_owners: dict[str, set[str]] = {}
    genus_groups: dict[str, set[str]] = {}
    for sci, entry in taxonomy.items():
        manual = [a for a in manual_aliases.get(sci, []) if a not in taxonomy]
        for name in [sci, *entry["scientific_name_aliases"], *manual]:
            name_owners.setdefault(name, set()).add(sci)
            genus_groups.setdefault(name.split()[0], set()).add(entry["taxon_group"])
    for genus, _ in avilist_subspecies:
        genus_groups.setdefault(genus, set()).add("Aves")
    xc_count = 0
    xc_rejected = 0
    for sci, entry in taxonomy.items():
        xc = _raw_lookup(xc_data, _entry_names(sci, entry))
        raw_xc = xc.get("xc_name") or ""
        method = xc_record_method(xc)
        matched = xc.get("matched_name") or ""
        xc_name = _validated_xc_name(sci, entry, raw_xc, name_owners, genus_groups,
                                     method, matched)
        if raw_xc and not xc_name:
            xc_rejected += 1
        if xc_name:
            entry["xc_name"] = xc_name
            id_sources.setdefault(sci, {})["xc_name"] = xc_name
            xc_count += 1
        # XC names are cross-references, not reviewed synonyms: kept apart from
        # scientific_name_aliases (which collectors read back as trusted
        # synonyms) and only merged into the published aliases.
        entry["xc_aliases"] = _own_aliases(sci, taxonomy, [
            a for a in [xc_name, *xc.get("aliases", [])]
            if a and _validated_xc_name(sci, entry, a, name_owners, genus_groups,
                                        method, matched)
        ])
    stats["ml_taxon_codes"] = ml_count
    stats["xc_names"] = xc_count
    stats["xc_rejected"] = xc_rejected
    stats["observationorg_ids"] = obsorg_count

    # Pass 3d: Wikidata identifiers (GBIF, NCBI, etc.). An ID found through an
    # alias counts only if that alias is trusted (reviewed, synonym source,
    # or a validated Xeno-Canto name).
    id_counts = {k: 0 for k in WIKIDATA_ID_FIELDS}
    wd_coverage = 0
    for sci, entry in taxonomy.items():
        names = _entry_names(sci, entry)
        wd_recs = [wikidata[n] for n in names if wikidata.get(n)]
        if not wd_recs:
            continue
        wd_coverage += 1
        trusted = (set(names) | set(entry["scientific_name_aliases"])
                   | set(entry.get("xc_aliases", []))
                   | {a for a in manual_aliases.get(sci, []) if a not in taxonomy})
        for key in WIKIDATA_ID_FIELDS:
            if key in ("avibase_id", "birdlife_id") and entry["taxon_group"] != "Aves":
                continue
            for wd in wd_recs:
                val = wd.get(key, "")
                # Collector records which Wikidata name supplied each ID; an ID
                # reached through a name we no longer trust is discarded.
                via = (wd.get("id_sources") or {}).get(key)
                if not val or (via and via not in trusted):
                    continue
                entry[key] = val
                id_sources.setdefault(sci, {})[key] = via or sci
                id_counts[key] += 1
                break
    stats["wikidata_ids"] = id_counts
    stats["wikidata_coverage"] = wd_coverage
    stats["gbif_accepted"] = _accept_gbif_keys(taxonomy, wikidata)

    stats["shared_ids_dropped"] = _resolve_shared_identifiers(taxonomy, id_sources)

    # Localized names: every source offers candidates per locale, ranked
    # AviList > eBird > iNat > Wikidata; conflicts are resolved below.
    for entry in taxonomy.values():
        entry["_name_candidates"] = {
            loc: [(NAME_SOURCE_RANK[entry["common_name_sources"].get(loc, "inat")], name)]
            for loc, name in entry["common_names"].items()
        }

    # Pass 4: eBird common names (non-English locales; AviList is authority
    #          for English bird names, set in Pass 1)
    ebird_name_counts: dict[str, int] = {}
    if ebird_names:
        for sci, entry in taxonomy.items():
            code = entry.get("ebird_code", "")
            if not code:
                continue
            names = ebird_names.get(code, {})
            is_bird = entry.get("taxon_group") == "Aves"
            for loc, name in names.items():
                # Don't overwrite English name for birds — AviList is authority
                if loc == "en" and is_bird:
                    continue
                entry["common_names"][loc] = name
                entry["common_name_sources"][loc] = "ebird"
                entry["_name_candidates"].setdefault(loc, []).append(
                    (NAME_SOURCE_RANK["ebird"], name)
                )
                ebird_name_counts[loc] = ebird_name_counts.get(loc, 0) + 1
    stats["ebird_names"] = ebird_name_counts

    # Pass 5: Wikidata labels as fallback for all species
    wd_label_counts: dict[str, int] = {}
    for sci, entry in taxonomy.items():
        for name in _entry_names(sci, entry):
            for loc, label in (wikidata.get(name, {}).get("labels", {}) or {}).items():
                loc = LOCALE_NORMALIZE.get(loc, loc)
                if label in (sci, name) or label in entry["scientific_name_aliases"]:
                    continue
                entry["_name_candidates"].setdefault(loc, []).append(
                    (NAME_SOURCE_RANK["wikidata"], label)
                )
                if loc not in entry["common_names"]:
                    entry["common_names"][loc] = label
                    entry["common_name_sources"][loc] = "wikidata"
                    wd_label_counts[loc] = wd_label_counts.get(loc, 0) + 1
    stats["wikidata_labels"] = wd_label_counts

    # English stays authoritative per species (AviList / iNat display name).
    for entry in taxonomy.values():
        entry["_name_candidates"].pop("en", None)
        entry["_english_forms"] = {
            n.strip().lower() for n in [
                entry.get("preferred_common_name", ""), entry.get("common_name_alt", ""),
                entry.get("common_names", {}).get("en", ""),
                (ebird_names.get(entry.get("ebird_code", "")) or {}).get("en", ""),
                *entry.get("english_names", []),
            ] if n
        }
    stats["name_conflicts_resolved"] = _resolve_name_conflicts(
        taxonomy, {a for names in manual_aliases.values() for a in names}
    )
    for entry in taxonomy.values():
        entry.pop("_english_forms", None)

    # Pass 6: Default image selection
    # Priority: iNat taxon (licensed) → eBird → Wikimedia → iNat observation
    img_stats = {"inat": 0, "ebird": 0, "wikimedia": 0, "inat_obs": 0,
                 "none": 0}

    print("\n  Selecting default images...")

    # 6a: iNat taxon photo — only if CC-licensed
    for sci, entry in taxonomy.items():
        inat_rec = inat.get(entry["inat_key"], {}) if entry["inat_key"] else {}
        url = inat_rec.get("image_url", "")
        lic = inat_rec.get("image_license", "")
        author = _parse_image_author(inat_rec.get("image_attribution", ""), "inat")
        if url and lic in ACCEPTABLE_LICENSES and _attributable(author, lic):
            entry["image_url"] = url
            entry["image_author"] = author
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
                entry["image_license"] = "© Macaulay Library"
                asset_match = re.search(r'/asset/(\d+)', eb["url"])
                asset_id = asset_match.group(1) if asset_match else ""
                entry["image_source"] = f"Macaulay Library ML{asset_id}"
                img_stats["ebird"] += 1

    # 6c: Wikimedia Commons (from wikidata_data.json)
    all_species = set(taxonomy)
    for sci, entry in taxonomy.items():
        if entry["image_url"]:
            continue
        own = set(_entry_names(sci, entry)) | set(entry.get("scientific_name_aliases", []))
        wd_img = next(
            (img for n in _entry_names(sci, entry)
             if (img := _usable_wikimedia_image(wikidata.get(n, {})))
             and not _file_names_other_species(img["url"], sci, own, all_species)),
            None,
        )
        if wd_img:
            entry["image_url"] = wd_img["url"]
            entry["image_author"] = _parse_image_author(
                wd_img.get("attribution", ""), "wikimedia")
            entry["image_license"] = _normalize_license(wd_img.get("license", ""))
            entry["image_source"] = "Wikimedia"
            img_stats["wikimedia"] += 1

    # 6d: iNat observation photo (CC-licensed fallback from observations API)
    for sci, entry in taxonomy.items():
        if entry["image_url"]:
            continue
        inat_rec = inat.get(entry["inat_key"], {}) if entry["inat_key"] else {}
        obs_photo = inat_rec.get("obs_photo")
        if (obs_photo and obs_photo.get("url") and _attributable(
                _parse_image_author(obs_photo.get("attribution", ""), "inat"),
                obs_photo.get("license", ""))):
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

    sound_classes = load_sound_classes()
    taxonomy.update(sound_classes)
    stats["sound_classes"] = len(sound_classes)

    return taxonomy, stats


def print_taxonomy_stats(taxonomy: dict, stats: dict):
    """Print summary statistics for the taxonomy build."""
    matched_birds = sum(stats[k] for k in BIRD_MATCH_RANK)
    total_birds = matched_birds + stats["avilist_only"] + stats["split_override"]
    total = len(taxonomy)
    inat_birds = matched_birds + stats["folded_birds"] + stats.get("excluded_bird_no_avilist", 0)

    print(f"\n  Total entries: {total}")
    print(f"  Birds:         {total_birds}")
    print(f"    Direct match:     {stats['direct']}")
    print(f"    Common name:      {stats['common_name']}")
    print(f"    Wikidata code:    {stats['wikidata']}")
    print(f"    eBird name:       {stats['ebird']}")
    print(f"    AviList subsp.:   {stats['subspecies']}")
    print(f"    AviList only:     {stats['avilist_only']} (no iNat ID)")
    print(f"    Kept split:       {stats['split_override']} (overrides/bird_splits.csv)")
    print(f"    Folded:           {stats['folded_birds']} iNat taxa merged into an AviList species")
    print(f"    Match rate:       {matched_birds + stats['folded_birds']}/{inat_birds} "
          f"iNat birds ({100 * (matched_birds + stats['folded_birds']) / max(1, inat_birds):.1f}%)")
    print(f"  Non-birds:     {stats['non_bird']}")
    if stats.get("sound_classes"):
        print(f"  Sound classes: {stats['sound_classes']}")
    if stats.get("excluded_inactive"):
        print(f"  Excluded:      {stats['excluded_inactive']} taxa retired on iNat (swapped/merged)")
    if stats.get("excluded_non_species"):
        print(f"  Excluded:      {stats['excluded_non_species']} non-species iNat taxa")
    if stats.get("excluded_extinct"):
        print(f"  Excluded:      {stats['excluded_extinct']} extinct species")
    if stats.get("excluded_bird_no_avilist"):
        print(f"  Excluded:      {stats['excluded_bird_no_avilist']} birds not on AviList")
    if stats.get("excluded_group"):
        print(f"  Excluded:      {stats['excluded_group']} species from disabled taxon groups")

    codes = stats.get("ebird_codes", {})
    print(f"\n  eBird codes: {sum(codes.values())} "
          f"({', '.join(f'{k}: {v}' for k, v in codes.items())})")

    # Wikidata identifiers
    wd_ids = stats.get("wikidata_ids", {})
    wd_cov = stats.get("wikidata_coverage", 0)
    print(f"\n  Wikidata identifiers ({wd_cov}/{total} entries found):")
    for key, count in wd_ids.items():
        print(f"    {key}: {count}")
    print(f"    GBIF synonym keys replaced by the accepted key: {stats.get('gbif_accepted', 0)}")
    dropped = stats.get("shared_ids_dropped", {})
    if dropped:
        print(f"    dropped as shared by several species: "
              f"{', '.join(f'{k}: {v}' for k, v in dropped.items())}")
    print(f"  Xeno-Canto names: {stats.get('xc_names', 0)} "
          f"({stats.get('xc_rejected', 0)} rejected as another species' name)")
    print(f"  Localized names re-assigned/dropped as conflicting: "
          f"{stats.get('name_conflicts_resolved', 0)}")

    # Common name coverage
    coverage = stats.get("coverage", {})
    ebird_n = stats.get("ebird_names", {})
    wd_labels = stats.get("wikidata_labels", {})
    n_locales = len(coverage)
    top_n = 30
    print(f"\n  Common name coverage ({total} entries, "
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
    print(f"\n  Default images ({total_with_img}/{total} entries):")
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


def _bn_id_ranges() -> dict[str, tuple[int, int]]:
    """BirdNET ID ranges per record kind from config.yml."""
    cfg = load_config().get("birdnet_ids", {})
    return {kind: tuple(cfg[kind]) for kind in ("birds", "non_birds", "sound_classes")}


def _bn_id_kind(rec: dict) -> str:
    if rec.get("record_type") == "sound_class":
        return "sound_classes"
    return "birds" if rec.get("taxon_group") == "Aves" else "non_birds"


def _assign_commonness(records: list[dict]) -> None:
    """commonness: percentile (0-100, one decimal) of a species' iNaturalist observation
    count within its taxon group, so groups with very different recording
    effort stay comparable (100 = most observed in its group; equal counts
    share a value). It reflects how often iNat users record a species, not
    true abundance. Sound classes get None."""
    groups: dict[str, list[dict]] = {}
    for rec in records:
        if rec.get("record_type") == "sound_class":
            rec["commonness"] = None
        else:
            groups.setdefault(rec.get("taxon_group", ""), []).append(rec)
    for recs in groups.values():
        counts = sorted(r.get("_observations") or 0 for r in recs)
        below: dict[int, int] = {}
        for i, c in enumerate(counts):
            below.setdefault(c, i)  # species with fewer observations
        span = max(len(counts) - 1, 1)
        for rec in recs:
            rec["commonness"] = round(100 * below[rec.get("_observations") or 0] / span, 1)


def _assign_bn_ids(records: list[dict], reassign: bool) -> None:
    """Assign BirdNET IDs within the configured range of each record kind.

    With reassign, every range is renumbered alphabetically by scientific
    name from its start; otherwise existing IDs stay and new species get the
    next number after the highest ID in their range. Fails if a range
    overflows or a kept ID lies outside its record's range.
    """
    ranges = _bn_id_ranges()
    bn_ids = {} if reassign else _load_bn_ids()
    by_kind: dict[str, list[dict]] = {kind: [] for kind in ranges}
    for rec in records:
        by_kind[_bn_id_kind(rec)].append(rec)
    new_count = 0
    for kind, recs in by_kind.items():
        lo, hi = ranges[kind]
        outside = [r["scientific_name"] for r in recs
                   if r["scientific_name"] in bn_ids and not lo <= bn_ids[r["scientific_name"]] <= hi]
        if outside:
            raise SystemExit(f"BirdNET IDs outside the {kind} range {lo}-{hi} for "
                             f"{len(outside)} records (e.g. {outside[0]}); rebuild with --reassign-ids")
        # IDs of removed species stay reserved: count the whole registry.
        next_id = max((v for v in bn_ids.values() if lo <= v <= hi), default=lo - 1) + 1
        for rec in sorted(recs, key=lambda r: r["scientific_name"]):
            if rec["scientific_name"] not in bn_ids:
                if next_id > hi:
                    raise SystemExit(f"BirdNET ID range for {kind} ({lo}-{hi}) is full")
                bn_ids[rec["scientific_name"]] = next_id
                next_id += 1
                new_count += 1
    for rec in records:
        rec["birdnet_id"] = f"BN{bn_ids[rec['scientific_name']]:05d}"
    if reassign or new_count:
        _save_bn_ids(bn_ids)
    counts = ", ".join(f"{kind}: {len(recs)}" for kind, recs in by_kind.items())
    verb = "reassigned" if reassign else f"{new_count} new;"
    print(f"  BirdNET IDs: {verb} {counts}")


def _save_bn_ids(bn_ids: dict[str, int]) -> None:
    """Save BirdNET species ID mapping (atomic write)."""
    tmp = BN_IDS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bn_ids, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BN_IDS_FILE)


def _metadata_quality(record: dict) -> tuple[int, list[str], int]:
    """Score metadata completeness for build-time pruning."""
    score = 0
    flags: list[str] = []

    descriptions = record.get("descriptions", {}) or {}
    english = descriptions.get("en", "")
    english_words = word_count(english)
    if english_words >= 80:
        score += 30
    elif english_words >= 40:
        score += 22
    elif english:
        score += 10
        flags.append("short_english_description")
    else:
        flags.append("missing_english_description")

    rich_locale_count = sum(1 for text in descriptions.values() if word_count(text) >= 40)
    score += min(20, rich_locale_count * 2)
    if rich_locale_count <= 1:
        flags.append("few_localized_descriptions")

    if record.get("image"):
        score += 20
    else:
        flags.append("missing_image")

    id_count = sum(1 for field in ID_FIELDS if record.get(field))
    external_id_count = sum(1 for field in EXTERNAL_ID_FIELDS if record.get(field))
    score += min(20, id_count * 3)
    if external_id_count < 2:
        flags.append("weak_external_ids")

    if (
        record.get("common_name")
        and record.get("common_name") != record.get("scientific_name")
    ):
        score += 5
    else:
        flags.append("missing_common_name")

    if record.get("_observations"):
        score += 5

    return min(100, score), flags, external_id_count


def _apply_metadata_quality_filter(
    records: list[dict],
    quality_cfg: dict | None,
) -> list[dict]:
    if not (quality_cfg or {}).get("enabled", False):
        return records

    min_score = int(quality_cfg.get("min_score", 0) or 0)
    group_min_scores = {
        str(group): int(score or 0)
        for group, score in (quality_cfg.get("group_min_scores", {}) or {}).items()
    }
    drop_thin = bool(quality_cfg.get("drop_if_missing_image_and_description", False))
    keep_groups = {str(g) for g in quality_cfg.get("keep_groups", []) or []}
    max_thin_external_ids = int(
        quality_cfg.get("max_external_ids_for_thin_records", 1) or 0
    )
    report_path = str(quality_cfg.get("report_path", "") or "").strip()

    kept: list[dict] = []
    report_rows: list[dict] = []
    drop_reasons: Counter[str] = Counter()
    for record in records:
        score, flags, external_id_count = _metadata_quality(record)
        record["metadata_quality_score"] = score
        record["metadata_quality_flags"] = flags
        if record.get("record_type") == "sound_class":
            kept.append(record)
            report_rows.append({
                "action": "keep",
                "score": score,
                "scientific_name": record.get("scientific_name", ""),
                "common_name": record.get("common_name", ""),
                "taxon_group": record.get("taxon_group", ""),
                "flags": "|".join(flags),
                "drop_reasons": "",
                "threshold": 0,
                "external_id_count": external_id_count,
                "description_source": record.get("description_source", ""),
                "has_image": "1" if record.get("image") else "0",
            })
            continue

        reasons: list[str] = []
        threshold = group_min_scores.get(str(record.get("taxon_group") or ""), min_score)
        if record.get("taxon_group") in keep_groups:
            threshold = 0
        elif threshold and score < threshold:
            reasons.append(f"score_below_{threshold}")
        if (
            drop_thin
            and record.get("taxon_group") not in keep_groups
            and "missing_image" in flags
            and "missing_english_description" in flags
            and external_id_count <= max_thin_external_ids
        ):
            reasons.append("thin_record")

        action = "drop" if reasons else "keep"
        if reasons:
            drop_reasons.update(reasons)
        else:
            kept.append(record)

        report_rows.append({
            "action": action,
            "score": score,
            "scientific_name": record.get("scientific_name", ""),
            "common_name": record.get("common_name", ""),
            "taxon_group": record.get("taxon_group", ""),
            "flags": "|".join(flags),
            "drop_reasons": "|".join(reasons),
            "threshold": threshold,
            "external_id_count": external_id_count,
            "description_source": record.get("description_source", ""),
            "has_image": "1" if record.get("image") else "0",
        })

    if report_path and report_rows:
        path = ROOT / report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(report_rows[0]))
            writer.writeheader()
            writer.writerows(report_rows)
        print(f"  Metadata quality report: {path}")

    dropped = len(records) - len(kept)
    if dropped:
        print(f"  Metadata quality: dropped {dropped}/{len(records)} species")
        for reason, count in drop_reasons.most_common():
            print(f"    {reason}: {count}")
    else:
        print(f"  Metadata quality: kept all {len(records)} species")

    return kept


# Identifier formats; values that don't match (a COL key in gbif_id, a
# BirdLife page slug, a truncated Avibase ID) are dropped.
ID_FORMATS = {
    "gbif_id": re.compile(r"\d+"),
    "ncbi_id": re.compile(r"\d+"),
    "birdlife_id": re.compile(r"\d+"),
    "observationorg_id": re.compile(r"\d+"),
    "avibase_id": re.compile(r"[0-9A-F]{16}"),
    "ebird_code": re.compile(r"[a-z0-9]+"),
    "wikidata_qid": re.compile(r"Q\d+"),
}
_BINOMIAL_SHAPE_RE = re.compile(r"[A-Z][a-z]+ [a-z][a-z-]+")
# Localized "names" that are file names, catalogue numbers, or carry a
# disambiguation number ("कालो धनेश २") (Wikidata labels).
_JUNK_NAME_RE = re.compile(r"\.(?:jpe?g|png|gif|svg|tiff?)\b|\d{3,}|\s\d+$", re.IGNORECASE)
_SMALL_WORDS = {"a", "an", "and", "of", "the", "in", "on", "or", "de", "del", "des", "du", "la", "le",
                "el", "al", "von", "van", "der", "den", "di", "da", "do", "dos", "das", "y"}


def _title_case(name: str) -> str:
    """AviList-style capitalisation for English names ("chestnut short-tailed
    bat" → "Chestnut Short-tailed Bat"); never lowercases a letter."""
    words = name.split(" ")
    return " ".join(
        w if (i and w in _SMALL_WORDS) else w[:1].upper() + w[1:]
        for i, w in enumerate(words)
    )


def _dedupe_images(records: list[dict]) -> int:
    """Keep a photo on one species only (split species can inherit the
    parent's photo): the species named in the file, else the most observed."""
    owners: dict[str, list[dict]] = {}
    for rec in records:
        if rec.get("image"):
            owners.setdefault(rec["image"]["src"], []).append(rec)
    removed = 0
    for src, recs in owners.items():
        if len(recs) < 2:
            continue
        fname = src.rsplit("/", 1)[-1].lower().replace("%20", "_").replace(" ", "_")
        recs.sort(key=lambda r: (r["scientific_name"].lower().replace(" ", "_") not in fname,
                                 -(r.get("_observations") or 0)))
        for rec in recs[1:]:
            rec.update(image=None, image_author="", image_license="", image_source="",
                       image_crop_anchor=None)
            removed += 1
    return removed


# Phrases in a Wikipedia lead that introduce the species' English names.
_LEAD_NAME_PATTERNS = (
    r"^(?:The|An?) (?P<seg>[^().,;]{3,80}?) \(",
    r"^{sci},? the (?P<seg>[^().,;]{3,60}?),? (?:is|are|was)\b",
    r"\b(?:known|called) as (?:the |an? )?(?P<seg>[^.;()]{3,200}?)(?:,? (?:is|are|was)\b|[.;)])",
    r"\bcommon names?(?::| is| are| include)? (?:the |an? )?(?P<seg>[^.;()]{3,200}?)[.;)]",
    r"\bcalled (?:the |an? )?(?P<seg>[^.;()]{3,80}?) in English\b",
    r"\bcommon name for [^.]{0,40}? is (?:the |an? )?(?P<seg>[^.;()]{3,80}?)\.",
)
_LEAD_NAME_STOPWORDS = {"is", "are", "include", "who", "which", "that", "simply", "its", "his",
                        "her", "after", "named", "species", "genus", "family"}


def _lead_english_names(text: str, sci: str) -> list[str]:
    """English names a Wikipedia lead gives for the species ("The white-spotted
    tree frog (Boana albopunctata) is ...", "... also known as X or Y")."""
    lead = " ".join(re.split(r"(?<=[.!?])\s+", (text or "").strip().lstrip("\"' "))[:3])
    names: list[str] = []
    for pattern in _LEAD_NAME_PATTERNS:
        for match in re.finditer(pattern.replace("{sci}", re.escape(sci)), lead):
            for part in re.split(r",\s*(?:or\s+|and\s+)?|\s+or\s+|\s+and\s+", match.group("seg")):
                part = re.sub(r"^(?:the|an?)\s+", "", part.strip(" \"'"), flags=re.IGNORECASE)
                words = part.lower().split()
                if (1 <= len(words) <= 6 and re.fullmatch(r"[A-Za-z][A-Za-z' -]*[a-z]", part)
                        and not _LEAD_NAME_STOPWORDS & set(words)
                        and part.lower() not in (n.lower() for n in names)):
                    names.append(part)
    return names


def _resolve_display_name_collisions(records: list[dict], taxonomy: dict) -> int:
    """Give species that share an English display name distinct names.

    iNat sometimes gives two species the same preferred English name. Birds
    (AviList names) keep theirs. Among the others, a species whose English
    Wikipedia lead confirms the name keeps it, then one with no other English
    name, then the more-observed one. The rest take an unused English name
    from their Wikipedia lead or iNat, else fall back to the scientific name.
    Returns the number of renamed species.
    """
    groups: dict[str, list[dict]] = {}
    for rec in records:
        if rec.get("record_type") == "sound_class" or rec["common_name"] == rec["scientific_name"]:
            continue
        groups.setdefault(rec["common_name"].lower(), []).append(rec)
    used = {rec["common_name"].lower() for rec in records}
    renamed = 0
    for name, group in groups.items():
        if len(group) < 2:
            continue
        candidates: dict[str, list[str]] = {}
        attested: dict[str, list[str]] = {}
        for rec in group:
            sci = rec["scientific_name"]
            lead = (_lead_english_names(rec["descriptions"].get("en", ""), sci)
                    if rec.get("description_source") == "wikipedia" else [])
            attested[sci] = [_title_case(n) for n in lead]
            inat_names = taxonomy.get(sci, {}).get("english_names", [])
            names = attested[sci] + [_title_case(n) for n in inat_names]
            candidates[sci] = [n for n in names if is_clean_common_name(n)]
        # Spelling variants ("Northern Freetail Bat") count as the shared name.
        def variant(n: str) -> bool:
            return difflib.SequenceMatcher(None, n.lower(), name).ratio() >= 0.9

        def unused(sci: str) -> list[str]:
            return [n for n in candidates[sci] if n.lower() not in used and not variant(n)]

        group.sort(key=lambda r: (
            r.get("taxon_group") != "Aves",
            not any(variant(n) for n in attested[r["scientific_name"]]),
            bool(unused(r["scientific_name"])),
            -(r.get("_observations") or 0),
        ))
        for rec in group[1:]:
            if rec.get("taxon_group") == "Aves":
                continue
            alt = next(iter(unused(rec["scientific_name"])), None)
            if alt:
                used.add(alt.lower())
                rec["common_name"] = alt
                rec["common_names"]["en"] = alt
            else:
                rec["common_name"] = rec["scientific_name"]
                rec["common_names"].pop("en", None)
            renamed += 1
    return renamed


def _resolve_localized_name_collisions(records: list[dict]) -> int:
    """Keep each non-English localized name on one species.

    Name conflicts between sources are settled earlier by source rank; this
    breaks the remaining ties (two species with the same name from the same
    source): the species whose Wikipedia article in that locale has the name
    as its title keeps it, else the more-observed one. Returns the number of
    names dropped.
    """
    owners: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        if rec.get("record_type") == "sound_class":
            continue
        for loc, name in rec["common_names"].items():
            if loc != "en":
                owners.setdefault((loc, name.lower()), []).append(rec)
    dropped = 0
    for (loc, name), group in owners.items():
        if len(group) < 2:
            continue
        def title(rec: dict) -> str:
            url = (rec.get("wikipedia_urls") or {}).get(loc, "")
            return unquote(url.rsplit("/", 1)[-1]).replace("_", " ").lower()
        group.sort(key=lambda r: (title(r) != name, -(r.get("_observations") or 0)))
        for rec in group[1:]:
            rec["common_names"].pop(loc)
            dropped += 1
    return dropped


# Locales whose LLM texts open with "[article] Name (Binomial)" in Latin or
# Cyrillic script, so the species' name can be aligned with common_names.
_NAME_ALIGN_LOCALES = {"de", "fr", "es", "pt", "it", "nl", "pl", "sv", "da", "no", "fi",
                       "cs", "ru", "tr", "sw"}
_LEAD_ARTICLE_RE = r"(?:Der|Die|Das|Le|La|Les|L['’]|El|Los|Las|Il|Lo|Gli|I|O|A|Os|As|De|Het|Een)"
_VOWEL_START_RE = re.compile(r"[AEIOUÀ-ÆÈ-ÏÒ-ÖÙ-Ü]", re.IGNORECASE)


def _align_llm_species_name(text: str, loc: str, canonical: str, names: list[str]) -> str:
    """Make an LLM text call the species by its canonical localized name.

    Translations often invent a vernacular name ("Der Bolivien-Schlüpfer
    (Cranioleuca henricae)") instead of the established one ("La-Paz-
    Baumschlüpfer"). The opening name, and later exact repeats of it, are
    replaced; a French/Italian article is elided or restored as needed.
    """
    sci_alt = "|".join(re.escape(n) for n in names)
    m = re.match(rf"\s*(?:(?P<art>{_LEAD_ARTICLE_RE})(?:\s+|(?<=['’])))?"
                 rf"(?P<name>[^()\n]{{2,80}}?)\s*\([^()]*?\b(?:{sci_alt})\b", text)
    if not m:
        return text
    name = m.group("name").strip(" ,")
    # A longer opening phrase ("X, also known as Y") is left alone.
    if (name.lower() == canonical.lower() or name in names
            or "," in name or len(name.split()) > 5):
        return text
    art = m.group("art") or ""
    if loc in ("fr", "it") and art:
        vowel = bool(_VOWEL_START_RE.match(canonical))
        if art[-1] in "'’" and not vowel:
            art = ""
        elif art in ("Le", "La", "Il", "Lo") and vowel:
            art = "L'"
    lead = canonical if art else canonical[:1].upper() + canonical[1:]
    if art:
        lead = art + ("" if art.endswith(("'", "’")) else " ") + lead
    # Later repeats keep the case of their first letter (sentence starts).
    first = re.escape(name[:1])
    repeat = re.compile(rf"(?<!\w)(?:{first.upper()}|{first.lower()}){re.escape(name[1:])}")
    rest = repeat.sub(lambda r: (canonical[:1].upper() + canonical[1:]
                                 if r.group(0)[:1].isupper() else canonical),
                      text[m.end("name"):])
    return lead + rest


def _align_llm_species_names(records: list[dict]) -> int:
    """Align LLM descriptions with each species' localized common name."""
    changed = 0
    for rec in records:
        names = [rec["scientific_name"], *rec.get("scientific_name_aliases", [])]
        for loc, src in (rec.get("description_sources") or {}).items():
            canonical = rec["common_names"].get(loc)
            if not src.startswith("llm") or loc not in _NAME_ALIGN_LOCALES or not canonical:
                continue
            text = rec["descriptions"][loc]
            aligned = _align_llm_species_name(text, loc, canonical, names)
            if aligned != text:
                rec["descriptions"][loc] = aligned
                changed += 1
    return changed


def build_metadata(taxonomy: dict, ebird: dict, wiki: dict,
                   translate: dict = None,
                   manual_overrides: dict | None = None,
                   manual_aliases: dict[str, list[str]] | None = None,
                   reassign_ids: bool = False,
                   quality_cfg: dict | None = None,
                   avicommons: dict[str, dict] | None = None,
                   manual_common_aliases: dict[str, list[str]] | None = None) -> list[dict]:
    """Merge taxonomy + raw sources into final species records.

    Each record:
      scientific_name, common_name, taxon_group, common_names,
      descriptions, description_source, wikipedia_urls,
            image, image_author, image_license, image_source, avicommons_image,
      inat_id, ebird_code, gbif_id, ncbi_id, avibase_id, birdlife_id
    """
    records = []
    desc_sources: dict[str, int] = Counter()
    canonical_names = {
        str(name).lower(): str(name)
        for name in taxonomy
        if is_clean_scientific_name(str(name))
    }
    source_alias_claims: Counter[str] = Counter()
    for sci_name, tax in taxonomy.items():
        for alias in clean_aliases([*tax.get("scientific_name_aliases", []),
                                    *tax.get("xc_aliases", [])]):
            if alias != sci_name and canonical_names.get(alias.lower(), sci_name) == sci_name:
                source_alias_claims[alias.lower()] += 1

    if translate is None:
        translate = {}
    if manual_overrides is None:
        manual_overrides = {}
    if manual_aliases is None:
        manual_aliases = {}
    if avicommons is None:
        avicommons = {}
    if manual_common_aliases is None:
        manual_common_aliases = {}

    image_prefix = image_url_prefix()
    min_english_words = int(load_config().get("descriptions", {}).get("min_english_words", 40))
    wrong_article = 0
    invalid_ids = 0
    avicommons_rejected: list[str] = []
    canonical_set = set(taxonomy)
    # An English article linked from several species stays with the species
    # it names first (the others are usually splits linking the parent).
    article_claims: dict[str, list[tuple[int, str]]] = {}
    for sci, tax in taxonomy.items():
        wp = _raw_lookup(wiki, _entry_names(sci, tax))
        url = (wp.get("wikipedia_urls") or {}).get("en")
        text = (wp.get("extract") or "").lower()
        if url and text:
            positions = [text.find(n.lower()) for n in [sci, *tax.get("scientific_name_aliases", [])]]
            positions = [p for p in positions if p >= 0]
            article_claims.setdefault(url, []).append((min(positions) if positions else 10**9, sci))
    article_owner = {url: min(claims)[1] for url, claims in article_claims.items() if len(claims) > 1}
    # eBird pages are scraped per code; a code is the stable link even when
    # the scientific name the page was fetched under has changed.
    ebird_by_code: dict[str, dict] = {}
    for rec in ebird.values():
        code = rec.get("ebird_code")
        if code and (rec.get("description") or code not in ebird_by_code):
            ebird_by_code[code] = rec

    for sci_name, tax in taxonomy.items():
        if is_sound_class_record(tax):
            if not is_clean_sound_class_name(sci_name):
                continue
        elif not is_clean_scientific_name(sci_name):
            continue
        names = _entry_names(sci_name, tax)
        wp = _raw_lookup(wiki, names)
        eb = ebird_by_code.get(tax.get("ebird_code", ""), {})
        cl = _raw_lookup(translate, names)
        identity_names = clean_aliases([
            *names, *tax.get("scientific_name_aliases", []), *tax.get("xc_aliases", []),
            *manual_aliases.get(sci_name, []),
        ])
        url = (wp.get("wikipedia_urls") or {}).get("en")
        if url in article_owner and article_owner[url] != sci_name:
            wp, cl = {}, {}
            wrong_article += 1
        wp_english = wp.get("extract") or wp.get("extracts", {}).get("en", "")
        if wp_english and (not describes_species(
                wp_english, wp.get("title", ""), identity_names,
                tax.get("preferred_common_name", ""), canonical_set)
                or names_other_species(wp_english, identity_names, canonical_set)):
            # The linked article is about another taxon: drop it, its other
            # locales (langlinks of the same article) and LLM text made from it.
            wp, cl = {}, {}
            wrong_article += 1

        # Descriptions: locale → text.
        # Base layer: Wikipedia (multi-locale) or eBird (English-only).
        # Claude overlay: replaces only the locales it provides.
        descriptions: dict[str, str] = {}
        description_sources: dict[str, str] = {}
        description_source = ""
        wikipedia_urls: dict[str, str] = {}

        # Wikipedia: use English extract independently from locale extracts.
        # Locale-only Wikipedia data must not block English fallback sources.
        # Older collector runs stored section headings and reference tails,
        # and some unlabelled machine translations: a locale text needs an
        # article URL or a provenance label (utils.translate) to be used.
        wp_extract_sources = wp.get("extract_sources", {}) or {}
        wp_known = set(wp.get("wikipedia_urls", {}) or {}) | set(wp_extract_sources)
        wp_extracts = {loc: clean_description_text(t) for loc, t in wp.get("extracts", {}).items()
                       if loc in wp_known}
        wp_en = clean_description_text(wp.get("extract") or "") or wp_extracts.get("en", "")
        # A disambiguation page ("Ruff may refer to:") is a list, not a
        # description; skip it here and in every source below.
        if wp_en and not is_disambiguation_text(wp_en):
            descriptions["en"] = wp_en
            description_sources["en"] = wp_extract_sources.get("en") or "wikipedia"

        for loc, text in wp_extracts.items():
            # A locale article about a sister species (wrong langlink or an
            # older lumped concept) is dropped; LLM translation fills the gap.
            if (loc != "en" and text and not is_disambiguation_text(text)
                    and not names_other_species(text, identity_names, canonical_set)):
                descriptions[loc] = text
                description_sources[loc] = wp_extract_sources.get(loc) or "wikipedia"
        # Links follow the texts: a locale whose article text was rejected
        # (disambiguation page, sister species) links to the wrong article.
        rejected = {
            loc for loc, text in {**wp_extracts, "en": wp_en}.items()
            if text and loc not in descriptions
        }
        for loc, url in wp.get("wikipedia_urls", {}).items():
            if url and loc not in rejected:
                # Wikipedia's canonical form writes spaces as underscores.
                wikipedia_urls[loc] = url.replace("%20", "_").replace(" ", "_")

        if (not descriptions.get("en") and eb.get("description")
                and not EBIRD_BOILERPLATE.match(eb["description"])):
            descriptions["en"] = eb["description"]
            description_sources["en"] = "ebird"

        # LLM overlay — replace only the locales the LLM provides
        translate_locales: list[str] = []
        cl_extracts = cl.get("extracts", {})
        fallback_locales = set(cl.get("fallback_locales", []))
        if cl.get("fallback"):
            fallback_locales.update(cl_extracts)
        translated = cl.get("translated_from", {}) or {}
        for loc, text in cl_extracts.items():
            # A machine translation made while Wikipedia lacked this locale
            # yields to native Wikipedia text added since.
            native = descriptions.get(loc, "")
            if loc in translated and word_count(native) >= max(25, word_count(text) // 2):
                continue
            # A fallback was written for a missing or stub article; a
            # substantive native text (e.g. an article found later) replaces it.
            if loc in fallback_locales and word_count(native) >= min_english_words:
                continue
            text = clean_description_text(text)
            # Guard against malformed locale keys (e.g. an LLM echoing the
            # literal "locale" placeholder from a prompt example).
            if (text and loc in LOCALE_NAMES and not is_disambiguation_text(text)
                    and not names_other_species(text, identity_names, canonical_set)):
                descriptions[loc] = text
                description_sources[loc] = (
                    "llm_fallback" if loc in fallback_locales else "llm"
                )
                translate_locales.append(loc)

        description_source = description_sources.get("en", "")
        if not description_source and descriptions:
            description_source = next(iter(description_sources.values()), "")

        desc_sources[description_source or "none"] += 1
        override = manual_overrides.get(sci_name, {})
        image_url = override.get("image_url") or tax.get("image_url", "")
        image_author = override.get("image_author") or tax.get("image_author", "")
        image_author = re.sub(r"\s+", " ", image_author).strip()
        image_license = override.get("image_license") or tax.get("image_license", "")
        image_source = override.get("image_source") or tax.get("image_source", "")
        image = None
        if image_url and not _attributable(image_author, image_license):
            image_url = image_author = image_license = image_source = ""
        if image_url:
            encoded_sci = quote(sci_name, safe='')
            image = {
                "src": image_url,
                "thumb": f"{image_prefix}/api/image/{encoded_sci}?size=thumb",
                "medium": f"{image_prefix}/api/image/{encoded_sci}?size=medium",
            }

        raw_common_name = " ".join(tax.get("preferred_common_name", "").replace("\xa0", " ").split())
        # Non-bird names (iNat) follow AviList-style capitalisation too.
        if (raw_common_name and tax.get("record_type") != "sound_class"
                and (raw_common_name == raw_common_name.lower() or tax.get("taxon_group") != "Aves")):
            raw_common_name = _title_case(raw_common_name)
        common_name = raw_common_name if is_clean_common_name(raw_common_name) else sci_name
        raw_common_name_alt = tax.get("common_name_alt", "")
        common_name_alt = (
            raw_common_name_alt if is_clean_common_name(raw_common_name_alt) else ""
        )
        common_names = {
            loc: name
            for loc, name in (
                (loc, " ".join(str(name).replace("\xa0", " ").split()))
                for loc, name in (tax.get("common_names", {}) or {}).items()
            )
            if is_clean_common_name(name) and not _JUNK_NAME_RE.search(name)
        }
        # The English entry is the display name (none when that falls back to
        # the scientific name: such entries are synonyms or other languages).
        if is_sound_class_record(tax):
            if common_names.get("en", "").lower() == common_name.lower():
                common_names["en"] = common_name
        elif common_name != sci_name:
            common_names["en"] = common_name
        else:
            common_names.pop("en", None)
        if is_sound_class_record(tax):
            aliases = clean_common_aliases(tax.get("scientific_name_aliases", []))
        else:
            source_aliases = clean_aliases([*tax.get("scientific_name_aliases", []),
                                            *tax.get("xc_aliases", [])])
            source_aliases = [
                alias for alias in source_aliases
                if alias != sci_name
                and canonical_names.get(alias.lower(), sci_name) == sci_name
                and source_alias_claims[alias.lower()] == 1
            ]
            aliases = clean_aliases([*source_aliases, *manual_aliases.get(sci_name, [])])
            # Drop aliases that equal this species' own name or are the canonical
            # name of a different species — catches manual aliases that shadow taxonomy keys.
            aliases = [
                a for a in aliases
                if a != sci_name
                and canonical_names.get(a.lower(), sci_name) == sci_name
            ]

        record = {
            "birdnet_id": None,
            "scientific_name": sci_name,
            "common_name": common_name,
            "common_name_alt": common_name_alt,
            "taxon_group": tax.get("taxon_group", ""),
            "record_type": tax.get("record_type", "species"),
            "common_names": common_names,
            "common_name_aliases": clean_common_aliases([
                *tax.get("common_name_aliases", []),
                *manual_common_aliases.get(sci_name, []),
            ]),
            "descriptions": descriptions,
            "description_source": description_source,
            "description_sources": description_sources,
            "translate_locales": sorted(translate_locales),
            "wikipedia_urls": wikipedia_urls,
            "scientific_name_aliases": aliases,
            "image": image,
            "image_author": image_author,
            "image_license": image_license,
            "image_source": image_source,
            "image_crop_anchor": override.get("image_crop_anchor"),
            "avicommons_image": None,
            "inat_id": tax.get("inat_id"),
            "ebird_code": tax.get("ebird_code", ""),
            "gbif_id": tax.get("gbif_id", ""),
            "ncbi_id": tax.get("ncbi_id", ""),
            "avibase_id": tax.get("avibase_id", ""),
            "birdlife_id": tax.get("birdlife_id", ""),
            "ml_taxon_code": tax.get("ml_taxon_code", ""),
            "xc_name": tax.get("xc_name", ""),
            "observationorg_id": tax.get("observationorg_id", ""),
            "wikidata_qid": tax.get("wikidata_qid", ""),
            "commonness": None,  # set once the final species set is known
            # iNat observation count: build-internal (quality score and
            # tie-breaks), not published.
            "_observations": tax.get("observations_count", 0),
        }
        for field, pattern in ID_FORMATS.items():
            if record.get(field) not in (None, "") and not pattern.fullmatch(str(record[field])):
                record[field] = ""
                invalid_ids += 1
        # Avicommons photo for the eBird code, kept only if it names this
        # species: its scientific name is ours or an alias (Avicommons follows
        # eBird, which can lag an AviList genus move), or its English name is ours.
        av = avicommons.get(record["ebird_code"])
        if av:
            if av["sci"] in identity_names or av["name"].casefold() in {
                    common_name.casefold(), common_name_alt.casefold()}:
                record["avicommons_image"] = {
                    k: av[k] for k in ("src", "thumb", "medium", "author", "license")
                }
            else:
                avicommons_rejected.append(f"{av['sci']} ({record['ebird_code']}) ≠ {sci_name}")
        records.append(record)

    records = _apply_metadata_quality_filter(records, quality_cfg)
    print(f"  Identifiers dropped for an invalid format: {invalid_ids}")
    print(f"  Images kept on one species only (shared photo): {_dedupe_images(records)} removed")
    renamed = _resolve_display_name_collisions(records, taxonomy)
    print(f"  Display names: {renamed} species renamed (name shared with another species)")
    # A manual common-name alias must not be another species' display name.
    name_owner = {r["common_name"].lower(): r["scientific_name"] for r in records}
    for sci, values in manual_common_aliases.items():
        for alias in values:
            owner = name_owner.get(alias.lower(), sci)
            if owner != sci:
                raise ValueError(
                    f"{MANUAL_COMMON_ALIASES_FILE}: alias '{alias}' for '{sci}' "
                    f"is the common name of '{owner}'"
                )
    print(f"  Localized names: {_resolve_localized_name_collisions(records)} dropped "
          "(same name on a more-observed species or the article's title species)")
    print(f"  LLM descriptions: {_align_llm_species_names(records)} aligned with the "
          "localized common name")

    # Assign BirdNET species IDs (persistent; ranges per record kind)
    _assign_bn_ids(records, reassign_ids)

    # Sort by BirdNET ID; drop build-internal fields
    records.sort(key=lambda r: r.get("birdnet_id") or "")
    _assign_commonness(records)
    for rec in records:
        rec.pop("_observations", None)

    # Print stats
    groups = Counter(r["taxon_group"] for r in records)
    n_locales = len(set(
        loc for r in records for loc in r.get("common_names", {}).keys()
    ))
    img_sources = Counter(
        (r.get("image_source", "") or "none").split(" ML")[0]
        for r in records
    )

    print(f"  {len(records)} entries")
    for g, n in sorted(groups.items()):
        print(f"    {g}: {n}")

    print(f"\n  Common names: {n_locales} locales")

    print(f"\n  Descriptions:")
    print(f"    dropped Wikipedia articles about another taxon: {wrong_article}")
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
    if avicommons:
        n_birds = sum(1 for r in records if r["taxon_group"] == "Aves")
        n_av = sum(1 for r in records if r.get("avicommons_image"))
        print(f"    Avicommons references: {n_av}/{n_birds} birds "
              f"({len(avicommons_rejected)} rejected: names another species)")
        for line in avicommons_rejected[:5]:
            print(f"      {line}")

    return records


def records_to_csv(records: list[dict], locales: list[str] | None = None) -> str:
    """Convert records to CSV without description excerpts.

    One common_name_<locale> column per configured locale (csv_export.locales).
    """
    if locales:
        top_locales = [str(loc) for loc in locales]
    else:
        locale_counts: Counter[str] = Counter()
        for r in records:
            locale_counts.update(r.get("common_names", {}).keys())
        top_locales = [loc for loc, _ in locale_counts.most_common(30)]

    base_cols = [
        "birdnet_id",
        "scientific_name", "common_name", "common_name_alt", "taxon_group",
        "record_type", "scientific_name_aliases", "common_name_aliases",
        "inat_id", "ebird_code", "gbif_id", "ncbi_id",
        "avibase_id", "birdlife_id", "ml_taxon_code", "xc_name",
        "observationorg_id", "wikidata_qid",
        "commonness",
        "description_source",
        "metadata_quality_score", "metadata_quality_flags",
        "image_url",
        "image_author", "image_license", "image_source",
        "avicommons_image_url", "avicommons_image_author", "avicommons_image_license",
    ]
    locale_cols = [f"common_name_{loc}" for loc in top_locales]
    fieldnames = base_cols + locale_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for rec in records:
        row = {k: rec.get(k, "") for k in base_cols}
        row["scientific_name_aliases"] = "|".join(rec.get("scientific_name_aliases", []))
        row["common_name_aliases"] = "|".join(rec.get("common_name_aliases", []))
        row["metadata_quality_flags"] = "|".join(rec.get("metadata_quality_flags", []))
        row["image_url"] = (rec.get("image") or {}).get("medium", "")
        av = rec.get("avicommons_image") or {}
        row["avicommons_image_url"] = av.get("medium", "")
        row["avicommons_image_author"] = av.get("author", "")
        row["avicommons_image_license"] = av.get("license", "")
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
    parser.add_argument("--strict-validation", action="store_true",
                        help="Fail if final metadata has release-gate audit findings")
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
        avilist_rows, avilist_subspecies = load_avilist(cfg)
        print(f"  iNaturalist: {len(inat)} species")
        print(f"  AviList:     {len(avilist_rows)} bird species")

        print("\nBuilding taxonomy...")
        allowed_groups = {g["name"] for g in cfg.get("taxon_groups", [])}
        taxonomy, stats = build_taxonomy(inat, avilist_rows, allowed_groups,
                                         avilist_subspecies, load_bird_splits())
        print_taxonomy_stats(taxonomy, stats)

        if not args.dry_run:
            save_json(taxonomy, TAXONOMY_FILE)
            size_mb = TAXONOMY_FILE.stat().st_size / 1024 / 1024
            print(f"\n  Saved: {TAXONOMY_FILE} ({size_mb:.1f} MB)")

    if args.dry_run:
        # Show excluded bird examples
        if not args.merge_only:
            if stats.get("excluded_bird_no_avilist"):
                print(f"\n  Excluded {stats['excluded_bird_no_avilist']} iNat birds not on AviList")
            if stats.get("excluded_extinct"):
                print(f"\n  Excluded {stats['excluded_extinct']} extinct species")
        return

    # Phase 2: Merge with descriptions → final output
    out_dir = ROOT / ("dev" if args.dev else "dist")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\nLoading enrichment data...")
    ebird = load_json(EBIRD_DATA_FILE)
    wiki = load_json(WIKI_DATA_FILE)
    llm_cfg = cfg.get("llm") or cfg.get("claude", {})
    llm_enabled = bool(llm_cfg.get("enabled", False))
    translate = load_json(TRANSLATE_DATA_FILE) if llm_enabled else {}
    print(f"  eBird:     {len(ebird):>8} species")
    print(f"  Wikipedia: {len(wiki):>8} species")
    avicommons = load_avicommons(cfg)
    print(f"  Avicommons: {len(avicommons):>7} photos")
    if llm_enabled:
        print(f"  LLM:       {len(translate):>8} species")
    else:
        print("  LLM:       disabled")
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
    manual_aliases = load_manual_aliases()
    unknown_aliases = sorted(set(manual_aliases) - set(taxonomy))
    if unknown_aliases:
        joined = ", ".join(unknown_aliases[:10])
        if len(unknown_aliases) > 10:
            joined += f", ... (+{len(unknown_aliases) - 10} more)"
        raise ValueError(
            f"Manual aliases reference unknown species: {joined}"
        )
    print(f"  Aliases:    {sum(len(v) for v in manual_aliases.values()):>8} manual aliases")
    print_manual_alias_stats(manual_aliases)
    manual_common_aliases = load_manual_common_aliases()
    unknown_common = sorted(set(manual_common_aliases) - set(taxonomy))
    if unknown_common:
        raise ValueError(
            f"Manual common-name aliases reference unknown species: {', '.join(unknown_common[:10])}"
        )
    print(f"  Common aliases: {sum(len(v) for v in manual_common_aliases.values()):>4} manual "
          f"common-name aliases ({len(manual_common_aliases)} species)")

    print("\nMerging...")
    records = build_metadata(
        taxonomy,
        ebird,
        wiki,
        translate,
        manual_overrides,
        manual_aliases,
        reassign_ids=args.reassign_ids,
        quality_cfg=cfg.get("metadata_quality", {}),
        avicommons=avicommons,
        manual_common_aliases=manual_common_aliases,
    )

    print("\nAuditing final metadata...")
    desc_cfg = cfg.get("descriptions", {})
    audit = audit_records(
        records,
        min_english_words=desc_cfg.get("min_english_words", 40),
    )
    print_report(audit, sample_limit=5)
    if audit["alias_conflicts"] or audit["duplicate_ids"]:
        raise SystemExit(1)
    if args.strict_validation and has_failures(audit):
        raise SystemExit(1)

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
    csv_text = records_to_csv(records, cfg.get("csv_export", {}).get("locales"))
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
