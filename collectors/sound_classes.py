#!/usr/bin/env python3
"""
Collect non-species sound class labels and Wikimedia Commons images.

Input:  config.yml sound_classes
Output: raw_data/sound_classes.json

Usage:
    python -m collectors.sound_classes [--limit N] [--dry-run] [--new-only]
"""

import argparse
import json
import urllib.parse
import urllib.request

from config import load_config
from collectors._common import (
    RAW_DIR, USER_AGENT, LOCALE_NORMALIZE,
    is_clean_common_name, load_json, save_json,
)
from collectors.wikidata import check_commons_licenses

OUTPUT_FILE = RAW_DIR / "sound_classes.json"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


def _sparql_query(query: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        WIKIDATA_SPARQL,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["results"]["bindings"]
    except Exception as exc:
        print(f"  WARNING: Wikidata query failed: {exc}")
        return []


def _query_labels_and_images(qids: list[str]) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    if not qids:
        return {}, {}

    values = " ".join(f"wd:{qid}" for qid in qids)
    rows = _sparql_query(
        f"SELECT ?item ?label ?image WHERE {{\n"
        f"  VALUES ?item {{ {values} }}\n"
        f"  OPTIONAL {{ ?item rdfs:label ?label . FILTER(STRLEN(LANG(?label)) >= 2) }}\n"
        f"  OPTIONAL {{ ?item wdt:P18 ?image . }}\n"
        f"}}"
    )

    labels: dict[str, dict[str, str]] = {}
    images: dict[str, str] = {}
    for row in rows:
        item = row.get("item", {}).get("value", "")
        qid = item.rsplit("/", 1)[-1]
        label = row.get("label", {}).get("value", "")
        lang = row.get("label", {}).get("xml:lang", "")
        if label and lang:
            canonical = LOCALE_NORMALIZE.get(lang, lang)
            labels.setdefault(qid, {}).setdefault(canonical, label)
        image_url = row.get("image", {}).get("value", "")
        if image_url and qid not in images:
            images[qid] = urllib.parse.unquote(image_url.split("/")[-1])

    return labels, images


def _clean_aliases(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        alias = str(value or "").strip()
        if not alias or alias.lower() in seen or not is_clean_common_name(alias):
            continue
        seen.add(alias.lower())
        result.append(alias)
    return result


def _load_configured_classes() -> list[dict]:
    rows = []
    for entry in load_config().get("sound_classes", []) or []:
        name = str(entry.get("name") or "").strip()
        group = str(entry.get("group") or "").strip()
        qid = str(entry.get("wikidata_qid") or "").strip()
        if not name or not group:
            raise ValueError("sound_classes entries require name and group")
        if not is_clean_common_name(name):
            raise ValueError(f"Invalid sound class name: {name}")
        rows.append({
            "name": name,
            "group": group,
            "wikidata_qid": qid,
            "image_file": str(entry.get("image_file") or "").strip(),
            "aliases": _clean_aliases(entry.get("aliases", []) or []),
            "scientific_aliases": _clean_aliases(
                entry.get("scientific_aliases", []) or []
            ),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect configured non-species sound classes"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap new sound classes to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview work without fetching")
    parser.add_argument("--new-only", action="store_true",
                        help="Only process sound classes not already cached")
    args = parser.parse_args()

    configured = _load_configured_classes()
    existing = load_json(OUTPUT_FILE)
    targets = [row for row in configured if not args.new_only or row["name"] not in existing]
    if args.limit > 0:
        targets = targets[:args.limit]

    print("Sound class collector")
    print(f"  Configured: {len(configured)}")
    print(f"  Existing:   {len(existing)}")
    print(f"  To fetch:   {len(targets)}")

    if args.dry_run:
        for row in targets:
            print(f"    {row['name']} ({row['group']})")
        return

    qids = [row["wikidata_qid"] for row in targets if row.get("wikidata_qid")]
    labels_by_qid, image_files_by_qid = _query_labels_and_images(qids)
    qid_to_name = {row["wikidata_qid"]: row["name"] for row in targets if row.get("wikidata_qid")}
    image_files = {
        qid_to_name[qid]: filename
        for qid, filename in image_files_by_qid.items()
        if qid in qid_to_name
    }
    for row in targets:
        if row.get("image_file"):
            image_files[row["name"]] = row["image_file"]
    images = check_commons_licenses(image_files)

    data = dict(existing)
    for row in targets:
        name = row["name"]
        qid = row.get("wikidata_qid", "")
        common_names = {"en": name}
        for locale, label in labels_by_qid.get(qid, {}).items():
            if is_clean_common_name(label):
                common_names.setdefault(locale, label)
        image = images.get(name, {})
        data[name] = {
            "scientific_name": name,
            "common_name": name,
            "taxon_group": row["group"],
            "record_type": "sound_class",
            "wikidata_qid": qid,
            "common_names": common_names,
            "common_name_aliases": row["aliases"],
            "scientific_name_aliases": row["scientific_aliases"],
            "image_url": image.get("url", ""),
            "image_author": image.get("attribution", ""),
            "image_license": image.get("license", ""),
            "image_source": "Wikimedia" if image else "",
        }

    save_json(data, OUTPUT_FILE)
    print(f"  Saved: {OUTPUT_FILE} ({len(data)} sound classes)")


if __name__ == "__main__":
    main()
