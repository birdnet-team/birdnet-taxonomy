#!/usr/bin/env python3
"""Fill missing Wikipedia locale excerpts with public machine translation."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tqdm import tqdm

from config import load_config
from collectors._common import RAW_DIR, USER_AGENT, RateLimiter, load_json, save_json
from utils.description_quality import word_count

WIKI_DATA = RAW_DIR / "wikipedia_data.json"


class TranslationError(Exception):
    pass


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0].strip()
    return cut or text[:max_chars].strip()


def _translate_libre(
    endpoint: str,
    text: str,
    source: str,
    target: str,
    api_key: str = "",
) -> str:
    payload = {
        "q": text,
        "source": source,
        "target": target,
        "format": "text",
    }
    if api_key:
        payload["api_key"] = api_key

    req = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise TranslationError(str(exc)) from exc

    translated = data.get("translatedText")
    if not translated:
        raise TranslationError(f"Unexpected response from translation service: {data}")
    return str(translated).strip()


def _translate_mymemory(
    endpoint: str,
    text: str,
    source: str,
    target: str,
    api_key: str = "",
) -> str:
    params = {
        "q": text,
        "langpair": f"{source}|{target}",
    }
    if api_key:
        params["key"] = api_key

    req = Request(
        f"{endpoint}?{urlencode(params)}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise TranslationError(str(exc)) from exc

    status = int(data.get("responseStatus") or 0)
    translated = (data.get("responseData") or {}).get("translatedText")
    if status and status != 200:
        raise TranslationError(str(data.get("responseDetails") or data))
    if translated and "QUERY LENGTH LIMIT EXCEEDED" in str(translated):
        raise TranslationError(str(translated))
    if not translated:
        raise TranslationError(f"Unexpected response from translation service: {data}")
    return str(translated).strip()


def _translate(
    provider: str,
    endpoint: str,
    text: str,
    source: str,
    target: str,
    api_key: str = "",
) -> str:
    if provider == "mymemory":
        return _translate_mymemory(endpoint, text, source, target, api_key)
    if provider == "libretranslate":
        return _translate_libre(endpoint, text, source, target, api_key)
    raise TranslationError(f"Unsupported translation provider: {provider}")


def _target_locales(raw: str, default_locales: list[str]) -> list[str]:
    if not raw or raw == "all":
        candidates = default_locales
    else:
        candidates = [loc.strip() for loc in raw.split(",") if loc.strip()]

    locales: list[str] = []
    seen: set[str] = set()
    for loc in candidates:
        loc = loc.strip()
        if not loc or loc == "en" or loc in seen:
            continue
        seen.add(loc)
        locales.append(loc)
    return locales


def _collect_work(
    wiki: dict,
    locales: list[str],
    min_source_words: int,
) -> list[tuple[str, str, str]]:
    work: list[tuple[str, str, str]] = []
    for sci, rec in sorted(wiki.items()):
        extracts = rec.get("extracts", {}) or {}
        english = rec.get("extract") or extracts.get("en", "")
        if word_count(english) < min_source_words:
            continue
        for loc in locales:
            if extracts.get(loc):
                continue
            work.append((sci, loc, english))
    return work


def _batch_text(items: list[tuple[str, str, str]], max_chars: int) -> str:
    parts: list[str] = []
    for index, (_, _, english) in enumerate(items, start=1):
        marker = f"[[[{index}]]]"
        parts.append(f"{marker}\n{_truncate(english, max_chars)}")
    return "\n\n".join(parts)


def _parse_batch_text(text: str, expected: int) -> list[str] | None:
    marker_re = re.compile(r"\[\[\[(\d+)\]\]\]")
    matches = list(marker_re.finditer(text or ""))
    if len(matches) != expected:
        return None

    pieces: list[str] = []
    for pos, match in enumerate(matches):
        number = int(match.group(1))
        if number != pos + 1:
            return None
        start = match.end()
        end = matches[pos + 1].start() if pos + 1 < len(matches) else len(text)
        piece = text[start:end].strip()
        if not piece:
            return None
        pieces.append(piece)
    return pieces


def _make_batches(
    work: list[tuple[str, str, str]],
    batch_size: int,
    batch_max_chars: int,
) -> list[list[tuple[str, str, str]]]:
    batches: list[list[tuple[str, str, str]]] = []
    by_locale: dict[str, list[tuple[str, str, str]]] = {}
    for item in work:
        by_locale.setdefault(item[1], []).append(item)

    for loc in sorted(by_locale):
        current: list[tuple[str, str, str]] = []
        for item in by_locale[loc]:
            candidate = [*current, item]
            per_item_chars = max(80, batch_max_chars // max(1, len(candidate)) - 16)
            candidate_text = _batch_text(candidate, per_item_chars)
            if (
                current
                and (
                    len(candidate) > batch_size
                    or len(candidate_text) > batch_max_chars
                )
            ):
                batches.append(current)
                current = [item]
                continue
            current = candidate
        if current:
            batches.append(current)
    return batches


def _store_translation(
    wiki: dict,
    sci: str,
    loc: str,
    translated: str,
    source_label: str,
    service_name: str,
) -> None:
    rec = wiki.setdefault(sci, {})
    rec.setdefault("extracts", {})[loc] = translated
    rec.setdefault("extract_sources", {})[loc] = source_label
    rec.setdefault("translation", {}).setdefault(loc, {})
    rec["translation"][loc] = {
        "source_locale": "en",
        "service": service_name,
        "source": source_label,
    }


def main() -> None:
    cfg = load_config()
    wiki_cfg = cfg.get("wikipedia", {})
    desc_cfg = cfg.get("descriptions", {})
    tr_cfg = cfg.get("translation", {})

    default_rps = float(tr_cfg.get("rps", 0.3) or 0.3)
    default_save_every = int(tr_cfg.get("save_every", 25) or 25)
    default_max_chars = int(tr_cfg.get("max_chars", 3000) or 3000)
    default_batch_size = int(tr_cfg.get("batch_size", 1) or 1)
    default_batch_max_chars = int(tr_cfg.get("batch_max_chars", default_max_chars) or default_max_chars)
    default_min_source_words = int(desc_cfg.get("min_english_words", 40) or 40)

    parser = argparse.ArgumentParser(
        description="Translate missing Wikipedia excerpts from English"
    )
    parser.add_argument("--input", type=Path, default=WIKI_DATA)
    parser.add_argument("--output", type=Path, default=WIKI_DATA)
    parser.add_argument("--locales", default="all",
                        help="Comma-separated target locales, or all configured translation locales")
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum new translations to write (0 = all)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--endpoint", default=tr_cfg.get("endpoint", ""))
    parser.add_argument("--provider", default=tr_cfg.get("provider", "mymemory"),
                        choices=["mymemory", "libretranslate"])
    parser.add_argument("--service-name", default=tr_cfg.get("service_name", "LibreTranslate"))
    parser.add_argument("--api-key-env", default=tr_cfg.get("api_key_env", ""))
    parser.add_argument("--rps", type=float, default=0)
    parser.add_argument("--save-every", type=int, default=0)
    parser.add_argument("--max-chars", type=int, default=0,
                        help="Maximum English source chars sent per request")
    parser.add_argument("--batch-size", type=int, default=0,
                        help="Maximum excerpts to pack into one translation request")
    parser.add_argument("--batch-max-chars", type=int, default=0,
                        help="Maximum combined source chars per translation request")
    parser.add_argument("--min-source-words", type=int, default=0,
                        help="Skip English excerpts shorter than this")
    args = parser.parse_args()

    endpoint = str(args.endpoint or "").strip()
    if not endpoint:
        raise SystemExit("ERROR: translation.endpoint is required")
    rps = args.rps or default_rps
    save_every = args.save_every or default_save_every
    max_chars = args.max_chars or default_max_chars
    batch_size = max(1, args.batch_size or default_batch_size)
    batch_max_chars = max(1, args.batch_max_chars or default_batch_max_chars)
    min_source_words = args.min_source_words or default_min_source_words
    api_key = os.environ.get(args.api_key_env, "").strip() if args.api_key_env else ""

    wiki = load_json(args.input)
    if not wiki:
        raise SystemExit(f"ERROR: no Wikipedia data found at {args.input}")

    language_map = tr_cfg.get("language_map", {}) or {}
    default_locales = tr_cfg.get("locales") or wiki_cfg.get("locales", ["en"])
    locales = _target_locales(args.locales, default_locales)
    work = _collect_work(wiki, locales, min_source_words=min_source_words)
    if args.limit:
        work = work[:args.limit]

    print(f"Translation service: {args.service_name}")
    print(f"Endpoint: {endpoint}")
    print(f"Targets: {', '.join(locales)}")
    print(f"Work: {len(work)} missing excerpts")
    if args.dry_run:
        for sci, loc, text in work[:20]:
            print(f"  {sci} -> {loc} ({word_count(text)} source words)")
        if len(work) > 20:
            print(f"  ... and {len(work) - 20} more")
        return

    limiter = RateLimiter(rps)
    updated = 0
    errors = 0
    started = time.monotonic()
    source_label = f"Source: Wikipedia, translated by {args.service_name}"
    batches = _make_batches(work, batch_size=batch_size, batch_max_chars=batch_max_chars)
    print(f"Batches: {len(batches)} requests (max {batch_size} excerpts/request)")

    pbar = tqdm(total=len(work), desc="Translate", unit="excerpt")
    for batch in batches:
        loc = batch[0][1]
        target = language_map.get(loc, loc)
        per_item_chars = min(max_chars, max(80, batch_max_chars // len(batch) - 16))
        source_text = _batch_text(batch, per_item_chars)
        limiter.acquire()
        try:
            translated = _translate(
                args.provider,
                endpoint,
                source_text,
                source="en",
                target=target,
                api_key=api_key,
            )
        except TranslationError as exc:
            print(f"  ERROR batch {loc} ({len(batch)} excerpts): {exc}")
            pieces = None
        else:
            pieces = _parse_batch_text(translated, len(batch))

        if pieces is None and len(batch) > 1:
            pieces = []
            for sci, loc, english in batch:
                target = language_map.get(loc, loc)
                source_text = _truncate(english, max_chars)
                limiter.acquire()
                try:
                    single = _translate(
                        args.provider,
                        endpoint,
                        source_text,
                        source="en",
                        target=target,
                        api_key=api_key,
                    )
                except TranslationError as exc:
                    errors += 1
                    print(f"  ERROR {sci} [{loc}]: {exc}")
                    pieces.append("")
                    continue
                pieces.append(single)

        if pieces is None:
            errors += len(batch)
            pbar.update(len(batch))
            pbar.set_postfix(translated=updated, errors=errors)
            continue

        batch_updates = 0
        for (sci, loc, _), piece in zip(batch, pieces):
            if not piece:
                continue
            _store_translation(wiki, sci, loc, piece, source_label, args.service_name)
            updated += 1
            batch_updates += 1
        pbar.update(len(batch))
        pbar.set_postfix(translated=updated, errors=errors)

        if batch_updates and updated % save_every < batch_updates:
            save_json(wiki, args.output)
            elapsed = max(1.0, time.monotonic() - started)
            print(f"  Saved {updated} translations ({updated / elapsed:.2f}/s)")

    pbar.close()
    save_json(wiki, args.output)
    print(f"Done. translated={updated}, errors={errors}, output={args.output}")


if __name__ == "__main__":
    main()
