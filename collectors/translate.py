#!/usr/bin/env python3
"""
Translate and shorten species descriptions using an LLM (Gemini / OpenAI / Anthropic).

Reads Wikipedia extracts from wikipedia_data.json and:
  Phase 1 — Shortens excessively long extracts (>max_extract_words)
  Phase 2 — Translates missing locale extracts from the English source
  Phase 3 — English fallback descriptions from non-English Wikipedia sources

Output: raw_data/translate_data.json (incremental, resumable)

Usage:
    python -m collectors.translate [--limit N] [--dry-run] [--provider gemini]

API key is read from .env (GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY).
Provider is auto-detected from whichever key is present.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from config import load_config, LOCALE_NAMES
from collectors._common import (
    ROOT, RAW_DIR, setup_shutdown, is_shutting_down,
    load_json, save_json,
)
from utils.description_quality import word_count as _word_count
from utils.llm import call_llm, detect_provider, parse_json_response, PROVIDERS

setup_shutdown()

WIKI_DATA = RAW_DIR / "wikipedia_data.json"
OUTPUT_FILE = RAW_DIR / "translate_data.json"
JOURNAL_FILE = RAW_DIR / "translate_journal.jsonl"

# Set in main() based on config / CLI args
_provider: str = ""
_model: str = ""
_request_delay: float = 0.0
_request_gate = threading.Lock()
_next_request_time = 0.0


# ---------------------------------------------------------------------------
# Rate-limiting across worker threads
# ---------------------------------------------------------------------------

def _acquire_request_slot() -> None:
    global _next_request_time
    if _request_delay <= 0:
        return
    with _request_gate:
        now = time.monotonic()
        if now < _next_request_time:
            time.sleep(_next_request_time - now)
            now = time.monotonic()
        _next_request_time = now + _request_delay


def _llm(system: str, user: str, max_tokens: int) -> str | None:
    """Call the configured LLM with rate limiting. Returns text or None."""
    try:
        return call_llm(
            user=user,
            system=system,
            provider=_provider,
            model=_model,
            max_tokens=max_tokens,
            pre_call_fn=_acquire_request_slot,
        )
    except Exception as exc:
        tqdm.write(f"  LLM error: {exc}")
        return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars at a sentence boundary."""
    if not text or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in (". ", ".\n", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > max_chars // 2:
            return cut[: idx + 1]
    return cut.rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------------------
# Core: shorten + translate + fallback
# ---------------------------------------------------------------------------

def shorten_extracts(
    items: list[tuple[str, str, str]],
    target_words: int = 150,
    max_tokens: int = 16384,
) -> dict[str, dict[str, str]]:
    """Shorten excessively long extracts via LLM.

    items: [(scientific_name, locale, extract_text)]
    Returns: {scientific_name: {locale: shortened_text}}
    """
    if not items:
        return {}

    system = (
        "You are a concise natural history writer.\n\n"
        f"TASK: Shorten each extract to ~{target_words} words while preserving "
        "key facts: appearance, habitat, geographic range, and behaviour.\n\n"
        "RULES:\n"
        "- Keep the SAME language as the input\n"
        "- Single flowing paragraph, no bullet points, no markdown\n"
        "- Do not start with the species name\n"
        "- Do not add information not present in the source\n\n"
        "OUTPUT FORMAT — return ONLY valid JSON, no markdown fences:\n"
        '{"Scientific name": {"locale": "shortened text..."}, ...}'
    )
    entries = [f"- {sci} [{loc}]: {text}" for sci, loc, text in items]
    user = (
        f"Shorten these {len(items)} extracts to ~{target_words} words each.\n\n"
        + "\n\n".join(entries)
    )

    result = _llm(system, user, max_tokens)
    if not result:
        return {}

    parsed = parse_json_response(result, "shorten")
    out: dict[str, dict[str, str]] = {}
    for sci, locales in parsed.items():
        if isinstance(locales, dict):
            for loc, text in locales.items():
                if isinstance(text, str) and text.strip():
                    out.setdefault(sci, {})[loc] = text.strip()
    return out


def translate_extracts(
    items: list[tuple[str, str]],
    target_locales: list[str],
    max_source_chars: int = 3000,
    max_tokens: int = 16384,
) -> dict[str, dict[str, str]]:
    """Translate English extracts to missing target locales.

    items: [(scientific_name, english_extract)]
    Returns: {scientific_name: {locale: translation}}
    """
    locales = [l for l in target_locales if l != "en" and l in LOCALE_NAMES]
    if not locales or not items:
        return {}

    lang_list = ", ".join(f"{l} ({LOCALE_NAMES[l]})" for l in locales)
    system = (
        "You are a professional translator of natural history texts.\n\n"
        "TASK: Translate each species description to all requested languages.\n\n"
        "RULES:\n"
        "- Preserve meaning, tone, and approximate length\n"
        "- Use natural phrasing in each language\n"
        "- Single flowing paragraph, no bullet points, no markdown\n\n"
        "OUTPUT FORMAT — return ONLY valid JSON, no markdown fences:\n"
        '{"Scientific name": {"de": "German...", "fr": "French...", ...}, ...}'
    )
    entries = [f"- {sci}: {_truncate(text, max_source_chars)}" for sci, text in items]
    user = (
        f"Translate these {len(items)} species descriptions.\n"
        f"Languages: {lang_list}\n\n"
        + "\n\n".join(entries)
    )

    result = _llm(system, user, max_tokens)
    if not result:
        return {}

    parsed = parse_json_response(result, "translate")
    locale_set = set(locales)
    out: dict[str, dict[str, str]] = {}
    for sci, trans in parsed.items():
        if isinstance(trans, dict):
            filtered = {
                k: v for k, v in trans.items()
                if k in locale_set and isinstance(v, str) and v.strip()
            }
            if filtered:
                out[sci] = filtered
    return out


def fallback_english_descriptions(
    items: list[tuple[str, dict[str, str]]],
    target_words: int = 120,
    max_source_chars: int = 5000,
    max_tokens: int = 16384,
) -> dict[str, dict[str, str]]:
    """Create English descriptions from non-English Wikipedia sources."""
    if not items:
        return {}

    system = (
        "You are a careful natural history editor.\n\n"
        f"TASK: Create an English species description (~{target_words} words) "
        "from the provided Wikipedia source extracts.\n\n"
        "RULES:\n"
        "- Use ONLY facts present in the provided source extracts\n"
        "- One concise paragraph, no markdown, no citations\n"
        "- Preserve scientific uncertainty; do not add unsourced facts\n\n"
        "OUTPUT FORMAT — return ONLY valid JSON, no markdown fences:\n"
        '{"Scientific name": {"en": "English fallback text..."}, ...}'
    )
    entries = []
    for sci, sources in items:
        parts = [f"[{loc}] {_truncate(text, max_source_chars)}" for loc, text in sorted(sources.items())]
        entries.append(f"- {sci}:\n" + "\n".join(parts))
    user = (
        f"Create English fallback descriptions for these {len(items)} species.\n\n"
        + "\n\n".join(entries)
    )

    result = _llm(system, user, max_tokens)
    if not result:
        return {}

    parsed = parse_json_response(result, "fallback")
    out: dict[str, dict[str, str]] = {}
    for sci, locales in parsed.items():
        if not isinstance(locales, dict):
            continue
        text = locales.get("en")
        if isinstance(text, str) and text.strip():
            out[sci] = {"en": text.strip()}
    return out


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def _batch_by_char_budget(
    items: list, text_getter, char_budget: int, max_items: int
) -> list[list]:
    batches: list[list] = []
    batch: list = []
    used = 0
    for item in items:
        length = max(1, len(text_getter(item)))
        if batch and (len(batch) >= max(1, max_items) or used + length > max(1, char_budget)):
            batches.append(batch)
            batch = []
            used = 0
        batch.append(item)
        used += length
    if batch:
        batches.append(batch)
    return batches


def _missing_translation_locales(
    wiki_extracts: dict[str, str],
    llm_extracts: dict[str, str],
    target_locales: list[str],
) -> tuple[str, ...]:
    return tuple(
        loc for loc in target_locales
        if not wiki_extracts.get(loc) and not llm_extracts.get(loc)
    )


def _build_translation_batches(
    items: list[tuple[str, str, tuple[str, ...]]],
    max_batch_size: int,
    char_budget: int,
    max_source_chars: int,
) -> list[tuple[tuple[str, ...], list[tuple[str, str]]]]:
    grouped: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for sci, en_text, missing in items:
        grouped.setdefault(missing, []).append((sci, en_text))

    batches: list[tuple[tuple[str, ...], list[tuple[str, str]]]] = []
    sort_key = lambda item: (-len(item[1]), -len(item[0]), item[0])
    for missing, group in sorted(grouped.items(), key=sort_key):
        for batch in _batch_by_char_budget(
            group,
            text_getter=lambda item: _truncate(item[1], max_source_chars),
            char_budget=char_budget,
            max_items=max_batch_size,
        ):
            batches.append((missing, batch))
    return batches


def _merge_updates(target: dict, updates: dict[str, dict[str, str]]) -> None:
    for sci, extracts in updates.items():
        existing_extracts = target.setdefault(sci, {}).setdefault("extracts", {})
        for loc, text in extracts.items():
            if loc not in existing_extracts:
                existing_extracts[loc] = text


def _mark_fallback_locales(target: dict, updates: dict[str, dict[str, str]]) -> None:
    for sci, extracts in updates.items():
        entry = target.setdefault(sci, {})
        locs = set(entry.get("fallback_locales", []))
        locs.update(extracts)
        entry["fallback_locales"] = sorted(locs)


def _append_journal(updates: dict[str, dict[str, str]], fallback: bool = False) -> None:
    if not updates:
        return
    with JOURNAL_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"extracts": updates, "fallback": fallback}, ensure_ascii=False) + "\n")


def _load_existing_outputs() -> dict:
    existing = load_json(OUTPUT_FILE)
    if not JOURNAL_FILE.exists():
        return existing
    replayed = 0
    with JOURNAL_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                tqdm.write("  WARN: Skipping malformed journal line")
                continue
            if isinstance(entry, dict):
                extracts = entry.get("extracts") if "extracts" in entry else entry
                _merge_updates(existing, extracts or {})
                if entry.get("fallback"):
                    _mark_fallback_locales(existing, extracts or {})
                replayed += 1
    if replayed:
        tqdm.write(f"  Replayed {replayed} journal batches")
    return existing


def _checkpoint(existing: dict) -> None:
    save_json(existing, OUTPUT_FILE)
    if JOURNAL_FILE.exists():
        JOURNAL_FILE.unlink()


def _save_if_needed(existing: dict, pending: int, save_every: int) -> int:
    if pending >= save_every:
        _checkpoint(existing)
        return 0
    return pending


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    global _provider, _model, _request_delay

    cfg = load_config()
    # Accept both 'llm' (new) and 'claude' (legacy) config sections
    llm_cfg = cfg.get("llm") or cfg.get("claude", {})

    delay = llm_cfg.get("request_delay", 0.5)
    default_batch = llm_cfg.get("batch_size", 5)
    max_tokens = llm_cfg.get("max_tokens", 16384)
    max_words = llm_cfg.get("max_extract_words", 500)
    target_words = llm_cfg.get("target_words", 150)
    target_locales = llm_cfg.get("locales", ["en"])
    translate_workers = llm_cfg.get("translate_workers", 4)
    save_every = llm_cfg.get("save_every", 10)
    source_char_budget = llm_cfg.get("source_char_budget", 12000)
    max_source_chars = llm_cfg.get("max_source_chars", 3000)
    cfg_provider = str(llm_cfg.get("provider", "auto")).strip()
    cfg_model = str(llm_cfg.get("model", "")).strip()
    target_non_en = [l for l in target_locales if l != "en"]

    auto_provider = detect_provider() or ""
    provider_choices = list(PROVIDERS) + ["auto"]

    parser = argparse.ArgumentParser(
        description="Translate/shorten species descriptions via LLM (Gemini/OpenAI/Anthropic)"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--shorten-only", action="store_true")
    parser.add_argument("--translate-only", action="store_true")
    parser.add_argument("--fallback-missing", action="store_true",
                        help="Generate English descriptions from non-English sources")
    parser.add_argument("--fallback-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--save-every", type=int, default=0)
    parser.add_argument("--char-budget", type=int, default=0)
    parser.add_argument("--max-source-chars", type=int, default=0)
    parser.add_argument(
        "--provider", default="",
        choices=provider_choices,
        help=f"LLM provider (auto-detected: {auto_provider or 'none found'})",
    )
    parser.add_argument(
        "--model", default="",
        help="Model name (overrides provider default)",
    )
    args = parser.parse_args()

    if args.fallback_only:
        args.fallback_missing = True
        args.shorten_only = False
        args.translate_only = False

    # Resolve provider: CLI > config > auto-detect
    raw_provider = args.provider or cfg_provider
    _provider = raw_provider if raw_provider and raw_provider != "auto" else auto_provider
    if not _provider:
        raise SystemExit(
            "ERROR: No LLM API key found in .env. "
            "Add one of: GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY"
        )

    # Resolve model: CLI > config
    _model = args.model or cfg_model
    default_model = PROVIDERS[_provider][1]
    display_model = _model or default_model

    _request_delay = max(0.0, float(delay))

    batch_size = max(1, args.batch_size or default_batch)
    n_workers = max(1, args.workers or translate_workers)
    save_every = max(1, args.save_every or save_every)
    char_budget = max(1, args.char_budget or source_char_budget)
    max_source_chars = max(500, args.max_source_chars or max_source_chars)

    wiki = load_json(WIKI_DATA)
    if not wiki:
        raise SystemExit("ERROR: wikipedia_data.json not found. Run collectors/wikipedia.py first.")

    existing = _load_existing_outputs()

    n_existing_extracts = sum(len(v.get("extracts", {})) for v in existing.values())
    print(f"Provider:   {_provider} ({display_model})")
    print(f"Loaded {len(wiki)} species from Wikipedia")
    print(f"Existing LLM data: {len(existing)} species, {n_existing_extracts} extracts (will not be overwritten)")
    print(f"Target locales: {', '.join(target_locales)}")
    print(f"Shorten threshold: >{max_words} words → ~{target_words} words")

    # ── Phase 1: extracts needing shortening ──────────────────────────
    needs_shortening: list[tuple[str, str, str]] = []
    if not args.translate_only and not args.fallback_only:
        for sci, rec in wiki.items():
            for loc, text in rec.get("extracts", {}).items():
                if not text:
                    continue
                if existing.get(sci, {}).get("extracts", {}).get(loc):
                    continue
                if _word_count(text) > max_words:
                    needs_shortening.append((sci, loc, text))

    # ── Phase 2: species needing translation ──────────────────────────
    needs_translation: list[tuple[str, str, tuple[str, ...]]] = []
    if not args.shorten_only and not args.fallback_only and target_non_en:
        for sci, rec in wiki.items():
            en_text = rec.get("extract") or rec.get("extracts", {}).get("en")
            if not en_text:
                continue
            wp_ext = rec.get("extracts", {})
            cl_ext = existing.get(sci, {}).get("extracts", {})
            missing = _missing_translation_locales(wp_ext, cl_ext, target_non_en)
            if missing:
                needs_translation.append((sci, en_text, missing))

    # ── Phase 3: species needing English fallback ─────────────────────
    needs_fallback: list[tuple[str, dict[str, str]]] = []
    if args.fallback_missing:
        for sci, rec in wiki.items():
            wp_ext = rec.get("extracts", {})
            cl_ext = existing.get(sci, {}).get("extracts", {})
            if rec.get("extract") or wp_ext.get("en") or cl_ext.get("en"):
                continue
            sources = {loc: t for loc, t in wp_ext.items() if loc != "en" and t}
            if sources:
                needs_fallback.append((sci, sources))

    if args.limit:
        needs_shortening = needs_shortening[: args.limit]
        rem = max(0, args.limit - len(needs_shortening))
        needs_translation = needs_translation[:rem]
        rem = max(0, rem - len(needs_translation))
        needs_fallback = needs_fallback[:rem]

    shorten_batches = _batch_by_char_budget(
        needs_shortening, text_getter=lambda x: x[2],
        char_budget=char_budget, max_items=batch_size,
    )
    translation_batches = _build_translation_batches(
        needs_translation, max_batch_size=batch_size,
        char_budget=char_budget, max_source_chars=max_source_chars,
    )
    fallback_batches = _batch_by_char_budget(
        needs_fallback, text_getter=lambda x: "\n".join(x[1].values()),
        char_budget=char_budget, max_items=batch_size,
    )
    locale_signatures = {missing for _, _, missing in needs_translation}

    print(f"\n  Phase 1 — Shorten:   {len(needs_shortening)} extracts ({len(shorten_batches)} batches)")
    print(f"  Phase 2 — Translate: {len(needs_translation)} species ({len(translation_batches)} batches, {len(locale_signatures)} locale sets)")
    print(f"  Phase 3 — Fallback:  {len(needs_fallback)} species ({len(fallback_batches)} batches)")
    n_api_calls = len(shorten_batches) + len(translation_batches) + len(fallback_batches)
    print(f"  Estimated API calls: ~{n_api_calls}")

    if args.dry_run:
        if needs_shortening:
            print(f"\n  Extracts to shorten (>{max_words} words):")
            for sci, loc, text in needs_shortening[:20]:
                print(f"    {sci} [{loc}]: {_word_count(text)} words")
            if len(needs_shortening) > 20:
                print(f"    ... and {len(needs_shortening) - 20} more")
        if needs_translation:
            print(f"\n  Species to translate (first 20):")
            for sci, text, missing in needs_translation[:20]:
                wp_ext = wiki[sci].get("extracts", {})
                cl_ext = existing.get(sci, {}).get("extracts", {})
                have = sorted(set(wp_ext) | set(cl_ext))
                print(f"    {sci}: have={','.join(have)}, missing={','.join(missing)}")
            if len(needs_translation) > 20:
                print(f"    ... and {len(needs_translation) - 20} more")
        if needs_fallback:
            print(f"\n  English fallbacks needed (first 20):")
            for sci, sources in needs_fallback[:20]:
                print(f"    {sci}: sources={','.join(sorted(sources))}")
        return

    # ── Phase 1: Shorten ─────────────────────────────────────────────
    if needs_shortening:
        print(f"\nPhase 1: Shortening {len(needs_shortening)} extracts...")
        pbar = tqdm(total=len(needs_shortening), desc="Shorten", unit="ext")
        pending = 0
        for batch in shorten_batches:
            if is_shutting_down():
                break
            results = shorten_extracts(batch, target_words=target_words, max_tokens=max_tokens)
            updates: dict[str, dict[str, str]] = {}
            for sci, locs in results.items():
                for loc, text in locs.items():
                    updates.setdefault(sci, {})[loc] = text
            _merge_updates(existing, updates)
            _append_journal(updates)
            pbar.update(len(batch))
            pending = _save_if_needed(existing, pending + 1, save_every)
        pbar.close()
        if pending:
            _checkpoint(existing)
    else:
        print("\nPhase 1: No extracts need shortening")

    if is_shutting_down():
        return

    # ── Phase 2: Translate ────────────────────────────────────────────
    if needs_translation:
        print(f"\nPhase 2: Translating {len(needs_translation)} species ({n_workers} workers)...")
        pbar = tqdm(total=len(needs_translation), desc="Translate", unit="sp")
        pending = 0

        def _do_translate(missing_locales: tuple[str, ...], batch: list[tuple[str, str]]):
            return translate_extracts(
                batch, list(missing_locales),
                max_source_chars=max_source_chars, max_tokens=max_tokens,
            )

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_do_translate, missing, batch): (missing, batch)
                for missing, batch in translation_batches
            }
            for future in as_completed(futures):
                if is_shutting_down():
                    break
                missing, batch = futures[future]
                try:
                    results = future.result()
                except Exception as exc:
                    tqdm.write(f"  Batch failed [{','.join(missing)}]: {exc}")
                    pbar.update(len(batch))
                    continue

                updates: dict[str, dict[str, str]] = {}
                for sci, trans in results.items():
                    wp_ext = wiki.get(sci, {}).get("extracts", {})
                    for loc, text in trans.items():
                        cl_ext = existing.get(sci, {}).get("extracts", {})
                        if not wp_ext.get(loc) and not cl_ext.get(loc):
                            updates.setdefault(sci, {})[loc] = text
                _merge_updates(existing, updates)
                _append_journal(updates)
                pbar.update(len(batch))
                pending = _save_if_needed(existing, pending + 1, save_every)

        pbar.close()
        if pending:
            _checkpoint(existing)
    else:
        print("\nPhase 2: No translations needed")

    if is_shutting_down():
        return

    # ── Phase 3: English fallback ─────────────────────────────────────
    if needs_fallback:
        print(f"\nPhase 3: Generating {len(needs_fallback)} English fallbacks...")
        pbar = tqdm(total=len(needs_fallback), desc="Fallback", unit="sp")
        pending = 0
        for batch in fallback_batches:
            if is_shutting_down():
                break
            results = fallback_english_descriptions(
                batch, target_words=target_words,
                max_source_chars=max_source_chars, max_tokens=max_tokens,
            )
            updates: dict[str, dict[str, str]] = {}
            for sci, extracts in results.items():
                if "en" in extracts and not existing.get(sci, {}).get("extracts", {}).get("en"):
                    updates.setdefault(sci, {})["en"] = extracts["en"]
            _merge_updates(existing, updates)
            _mark_fallback_locales(existing, updates)
            _append_journal(updates, fallback=True)
            pbar.update(len(batch))
            pending = _save_if_needed(existing, pending + 1, save_every)
        pbar.close()
        if pending:
            _checkpoint(existing)
    elif args.fallback_missing:
        print("\nPhase 3: No English fallbacks needed")

    n_extracts = sum(len(v.get("extracts", {})) for v in existing.values())
    print(f"\nDone! {len(existing)} species, {n_extracts} total extracts in {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
