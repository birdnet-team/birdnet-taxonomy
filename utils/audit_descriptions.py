#!/usr/bin/env python3
"""Audit final species descriptions and write a focused CSV report."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from config import ROOT, load_config
from utils.description_quality import word_count

DEFAULT_METADATA = ROOT / "dist" / "species_metadata.json"
DEFAULT_REPORT = ROOT / "dev" / "description_audit.csv"


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return data


def audit_descriptions(records: list[dict[str, Any]],
                       min_english_words: int,
                       all_locales: bool = True) -> tuple[list[dict[str, Any]], Counter]:
    rows: list[dict[str, Any]] = []
    counts: Counter = Counter()
    for rec in records:
        sci = str(rec.get("scientific_name") or "")
        descriptions = rec.get("descriptions") if isinstance(rec.get("descriptions"), dict) else {}
        sources = rec.get("description_sources") if isinstance(rec.get("description_sources"), dict) else {}
        english = str(descriptions.get("en") or "")
        english_words = word_count(english)
        status = "ok"
        if not english:
            status = "missing_en"
        elif english_words < min_english_words:
            status = "short_en"

        counts[status] += 1
        counts[f"source_{sources.get('en') or rec.get('description_source') or 'none'}"] += 1

        if status != "ok":
            rows.append({
                "scientific_name": sci,
                "birdnet_id": rec.get("birdnet_id", ""),
                "taxon_group": rec.get("taxon_group", ""),
                "locale": "en",
                "status": status,
                "words": english_words,
                "source": sources.get("en") or rec.get("description_source", ""),
                "wikipedia_en_url": (rec.get("wikipedia_urls") or {}).get("en", ""),
                "available_description_locales": "|".join(sorted(descriptions)),
                "available_source_locales": "|".join(
                    f"{loc}:{src}" for loc, src in sorted(sources.items())
                ),
            })
        if all_locales:
            for loc, text in sorted(descriptions.items()):
                if loc == "en" or not text:
                    continue
                units = word_count(str(text))
                if units >= min_english_words:
                    continue
                counts["short_localized"] += 1
                rows.append({
                    "scientific_name": sci,
                    "birdnet_id": rec.get("birdnet_id", ""),
                    "taxon_group": rec.get("taxon_group", ""),
                    "locale": loc,
                    "status": f"short_{loc}",
                    "words": units,
                    "source": sources.get(loc, ""),
                    "wikipedia_en_url": (rec.get("wikipedia_urls") or {}).get("en", ""),
                    "available_description_locales": "|".join(sorted(descriptions)),
                    "available_source_locales": "|".join(
                        f"{src_loc}:{src}" for src_loc, src in sorted(sources.items())
                    ),
                })
    return rows, counts


def write_report(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scientific_name",
        "birdnet_id",
        "taxon_group",
        "locale",
        "status",
        "words",
        "source",
        "wikipedia_en_url",
        "available_description_locales",
        "available_source_locales",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    cfg = load_config()
    desc_cfg = cfg.get("descriptions", {})
    default_min = int(desc_cfg.get("min_english_words", 40))

    parser = argparse.ArgumentParser(description="Audit species description coverage")
    parser.add_argument("metadata", nargs="?", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--min-english-words", type=int, default=default_min)
    parser.add_argument("--english-only", action="store_true",
                        help="Only report English gaps/short excerpts")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 if missing or short English descriptions remain")
    args = parser.parse_args()

    records = load_records(args.metadata)
    rows, counts = audit_descriptions(
        records,
        args.min_english_words,
        all_locales=not args.english_only,
    )
    write_report(rows, args.output)

    print(f"Description audit: {len(records)} species")
    print(f"  ok: {counts['ok']}")
    print(f"  missing_en: {counts['missing_en']}")
    print(f"  short_en: {counts['short_en']}")
    print(f"  short_localized: {counts['short_localized']}")
    print(f"  report: {args.output}")
    if args.strict and (counts["missing_en"] or counts["short_en"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
