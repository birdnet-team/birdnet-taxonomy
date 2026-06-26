#!/usr/bin/env python3
"""Audit final species metadata for release readiness."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from collectors._common import is_clean_common_name, is_clean_scientific_name
from utils.description_quality import word_count

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_METADATA = ROOT / "dist" / "species_metadata.json"

REQUIRED_FIELDS = (
    "birdnet_id",
    "scientific_name",
    "common_name",
    "taxon_group",
    "description_source",
)
OPTIONAL_ID_FIELDS = (
    "inat_id",
    "ebird_code",
    "gbif_id",
    "ncbi_id",
    "avibase_id",
    "birdlife_id",
    "ml_taxon_code",
    "xc_name",
    "observationorg_id",
)


def _is_sound_class(record: dict[str, Any]) -> bool:
    return record.get("record_type") == "sound_class"

def load_records(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return data


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("scientific_name") or record.get("birdnet_id") or "<unknown>")


def audit_records(records: list[dict[str, Any]],
                  min_english_words: int = 40) -> dict[str, Any]:
    missing_required: dict[str, list[str]] = {field: [] for field in REQUIRED_FIELDS}
    missing_required["descriptions.en"] = []
    short_english: list[tuple[str, int]] = []
    bad_scientific: list[str] = []
    bad_common: list[tuple[str, str, str]] = []
    alias_conflicts: dict[str, list[str]] = {}
    optional_missing: dict[str, int] = {field: 0 for field in OPTIONAL_ID_FIELDS}
    image_missing = 0
    groups = Counter()
    desc_sources = Counter()

    alias_to_species: dict[str, set[str]] = defaultdict(set)
    canonical_names: dict[str, str] = {}

    for record in records:
        sci = str(record.get("scientific_name") or "")
        rec_id = _record_id(record)
        if sci:
            canonical_names[sci.lower()] = sci
        groups[str(record.get("taxon_group") or "")] += 1
        desc_sources[str(record.get("description_source") or "")] += 1

        for field in REQUIRED_FIELDS:
            if not record.get(field):
                missing_required[field].append(rec_id)

        descriptions = record.get("descriptions")
        english = ""
        if isinstance(descriptions, dict):
            english = str(descriptions.get("en") or "")
        if _is_sound_class(record):
            pass
        elif not english:
            missing_required["descriptions.en"].append(rec_id)
        else:
            words = word_count(english)
            if words < min_english_words:
                short_english.append((rec_id, words))

        if _is_sound_class(record):
            if not is_clean_common_name(sci):
                bad_scientific.append(rec_id)
        elif not is_clean_scientific_name(sci):
            bad_scientific.append(rec_id)

        for field in ("common_name", "common_name_alt"):
            value = record.get(field)
            if value and not is_clean_common_name(str(value)):
                bad_common.append((rec_id, field, str(value)))

        common_names = record.get("common_names")
        if isinstance(common_names, dict):
            for locale, value in common_names.items():
                if value and not is_clean_common_name(str(value)):
                    bad_common.append((rec_id, f"common_names.{locale}", str(value)))

        aliases = record.get("scientific_name_aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                alias_text = str(alias).strip()
                if alias_text:
                    if _is_sound_class(record):
                        if not is_clean_common_name(alias_text):
                            bad_scientific.append(f"{rec_id} alias:{alias_text}")
                    elif not is_clean_scientific_name(alias_text):
                        bad_scientific.append(f"{rec_id} alias:{alias_text}")
                    alias_to_species[alias_text.lower()].add(sci)

        common_aliases = record.get("common_name_aliases")
        if isinstance(common_aliases, list):
            for alias in common_aliases:
                alias_text = str(alias).strip()
                if alias_text and not is_clean_common_name(alias_text):
                    bad_common.append((rec_id, "common_name_aliases", alias_text))

        for field in OPTIONAL_ID_FIELDS:
            if not record.get(field):
                optional_missing[field] += 1

        image = record.get("image")
        if not (isinstance(image, dict) and (image.get("src") or image.get("medium"))):
            image_missing += 1

    for alias, species in alias_to_species.items():
        canonical = canonical_names.get(alias)
        if canonical:
            species.add(canonical)
        if len(species) > 1:
            alias_conflicts[alias] = sorted(species)

    missing_required = {
        field: values for field, values in missing_required.items() if values
    }

    return {
        "total": len(records),
        "groups": dict(sorted(groups.items())),
        "description_sources": dict(desc_sources.most_common()),
        "missing_required": missing_required,
        "short_english": short_english,
        "bad_scientific": bad_scientific,
        "bad_common": bad_common,
        "alias_conflicts": alias_conflicts,
        "optional_missing": optional_missing,
        "image_missing": image_missing,
    }


def print_report(report: dict[str, Any], sample_limit: int) -> None:
    print(f"Metadata audit: {report['total']} entries")
    print("Groups:")
    for group, count in report["groups"].items():
        print(f"  {group or '<blank>'}: {count}")

    print("\nDescription sources:")
    for source, count in report["description_sources"].items():
        print(f"  {source or '<blank>'}: {count}")

    print("\nRequired field gaps:")
    if not report["missing_required"]:
        print("  none")
    for field, names in report["missing_required"].items():
        print(f"  {field}: {len(names)}")
        for name in names[:sample_limit]:
            print(f"    {name}")
        if len(names) > sample_limit:
            print(f"    ... {len(names) - sample_limit} more")

    short = report["short_english"]
    print(f"\nShort English descriptions: {len(short)}")
    for name, words in short[:sample_limit]:
        print(f"  {name}: {words} words")
    if len(short) > sample_limit:
        print(f"  ... {len(short) - sample_limit} more")

    print(f"\nBad scientific names/aliases: {len(report['bad_scientific'])}")
    for name in report["bad_scientific"][:sample_limit]:
        print(f"  {name}")
    if len(report["bad_scientific"]) > sample_limit:
        print(f"  ... {len(report['bad_scientific']) - sample_limit} more")

    print(f"\nBad common names: {len(report['bad_common'])}")
    for sci, field, value in report["bad_common"][:sample_limit]:
        print(f"  {sci} {field}: {value}")
    if len(report["bad_common"]) > sample_limit:
        print(f"  ... {len(report['bad_common']) - sample_limit} more")

    conflicts = report["alias_conflicts"]
    print(f"\nAlias conflicts: {len(conflicts)}")
    for alias, species in list(conflicts.items())[:sample_limit]:
        print(f"  {alias}: {', '.join(species)}")
    if len(conflicts) > sample_limit:
        print(f"  ... {len(conflicts) - sample_limit} more")

    print("\nOptional ID gaps:")
    for field, count in report["optional_missing"].items():
        print(f"  {field}: {count}")
    print(f"  image: {report['image_missing']}")


def has_failures(report: dict[str, Any]) -> bool:
    return bool(
        report["missing_required"]
        or report["short_english"]
        or report["bad_scientific"]
        or report["bad_common"]
        or report["alias_conflicts"]
    )


def run_self_test() -> None:
    good = {
        "birdnet_id": "BN00001",
        "scientific_name": "Anas platyrhynchos",
        "common_name": "Mallard",
        "taxon_group": "Aves",
        "description_source": "wikipedia",
        "descriptions": {"en": " ".join(["duck"] * 40)},
        "scientific_name_aliases": ["Anas boschas"],
    }
    sound_class = {
        "birdnet_id": "BN00002",
        "scientific_name": "Human",
        "common_name": "Human",
        "taxon_group": "Anthropogenic",
        "record_type": "sound_class",
        "description_source": "",
        "descriptions": {},
        "scientific_name_aliases": ["Human vocal"],
        "common_name_aliases": ["human vocal", "human non-vocal"],
    }
    bad = {
        "birdnet_id": "",
        "scientific_name": "Anas platyrhynchos domesticus",
        "common_name": "Mallard (Domestic)",
        "taxon_group": "",
        "description_source": "",
        "descriptions": {"en": "Too short."},
        "scientific_name_aliases": ["Anas / bad"],
    }
    report = audit_records([good, sound_class, bad], min_english_words=40)
    assert "birdnet_id" in report["missing_required"]
    assert "taxon_group" in report["missing_required"]
    assert "description_source" in report["missing_required"]
    assert report["short_english"][0][0] == "Anas platyrhynchos domesticus"
    assert report["bad_scientific"]
    assert report["bad_common"]
    assert has_failures(report)
    print("Self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit final species metadata")
    parser.add_argument(
        "path", nargs="?", type=Path, default=DEFAULT_METADATA,
        help=f"Metadata JSON path (default: {DEFAULT_METADATA})",
    )
    parser.add_argument(
        "--min-english-words", type=int, default=40,
        help="Minimum acceptable English description word count",
    )
    parser.add_argument(
        "--sample-limit", type=int, default=10,
        help="Number of examples to print for each finding",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit with status 1 when release-gate findings are present",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run built-in helper tests and exit",
    )
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    records = load_records(args.path)
    report = audit_records(records, min_english_words=args.min_english_words)
    print_report(report, sample_limit=args.sample_limit)
    if args.strict and has_failures(report):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
