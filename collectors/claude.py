#!/usr/bin/env python3
from __future__ import annotations
"""
Translate and shorten species descriptions using the Claude API.

Reads Wikipedia extracts from wikipedia_data.json and:
  Phase 1 — Shortens excessively long extracts (>max_extract_words)
  Phase 2 — Translates missing locale extracts from the English source

Output: raw_data/claude_data.json (incremental, resumable)

Usage:
    python -m collectors.claude [--limit N] [--dry-run] [--batch-size N]

Requires ANTHROPIC_API_KEY in .env file.
"""

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from tqdm import tqdm

from config import load_config, LOCALE_NAMES
from collectors._common import (
    ROOT, RAW_DIR, setup_shutdown, is_shutting_down,
    load_json, save_json,
)

setup_shutdown()

WIKI_DATA = RAW_DIR / "wikipedia_data.json"
OUTPUT_FILE = RAW_DIR / "claude_data.json"
JOURNAL_FILE = RAW_DIR / "claude_journal.jsonl"

_api_key = None
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_request_delay = 0.0
_request_gate = threading.Lock()
_next_request_time = 0.0


# ── API layer ─────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """Load Anthropic API key from .env file."""
    global _api_key
    if _api_key:
        return _api_key

    env_file = ROOT / ".env"
    if not env_file.exists():
        raise RuntimeError(f".env file not found at {env_file}")

    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                _api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                return _api_key

    raise RuntimeError("ANTHROPIC_API_KEY not found in .env file")


def _acquire_request_slot():
    """Throttle Claude request start times across worker threads."""
    global _next_request_time
    if _request_delay <= 0:
        return
    with _request_gate:
        now = time.monotonic()
        if now < _next_request_time:
            time.sleep(_next_request_time - now)
            now = time.monotonic()
        _next_request_time = now + _request_delay


def _call_claude(system_prompt: str, user_message: str,
                 max_tokens: int = 4096) -> str | None:
    """Make a request to the Claude API. Returns text or None on error."""
    api_key = _load_api_key()
    cfg = load_config().get("claude", {})
    model = cfg.get("model", "claude-sonnet-4-20250514")

    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }).encode("utf-8")

    req = Request(CLAUDE_API_URL, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })

    for attempt in range(4):
        try:
            _acquire_request_slot()
            with urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("content", [])
                if content and content[0].get("type") == "text":
                    return content[0]["text"].strip()
                return None
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            if e.code == 429 or e.code >= 500:
                wait = min(2 ** (attempt + 1), 60)
                tqdm.write(f"  Claude {e.code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            tqdm.write(f"  Claude API error {e.code}: {body[:200]}")
            return None
        except (URLError, TimeoutError, OSError) as e:
            if attempt < 3:
                wait = min(2 ** (attempt + 1), 30)
                tqdm.write(f"  Claude timeout/network error, "
                           f"retrying in {wait}s... ({e})")
                time.sleep(wait)
                continue
            tqdm.write(f"  Claude API error after {attempt + 1} attempts: {e}")
            return None

    return None


# ── Text helpers ──────────────────────────────────────────────────────

def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, breaking at a sentence boundary."""
    if not text or len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    for sep in (". ", ".\n", "! ", "? "):
        idx = truncated.rfind(sep)
        if idx > max_chars // 2:
            return truncated[:idx + 1]
    return truncated.rsplit(" ", 1)[0] + "…"


def _parse_json_response(text: str) -> dict:
    """Parse a JSON response from Claude, with repair for common issues."""
    if not text:
        return {}

    cleaned = text.strip()

    # Strip markdown code fences
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()

    # Extract outermost JSON object
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start == -1 or brace_end <= brace_start:
        tqdm.write(f"  WARN: No JSON object in Claude response ({len(text)} chars)")
        return {}
    cleaned = cleaned[brace_start:brace_end + 1]

    # Attempt 1: parse as-is
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Attempt 2: fix trailing commas
    repaired = re.sub(r',\s*([}\]])', r'\1', cleaned)
    try:
        result = json.loads(repaired)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Attempt 3: handle truncated JSON
    truncated = repaired.rstrip()
    if truncated.count('"') % 2 != 0:
        last_quote = truncated.rfind('"')
        truncated = truncated[:last_quote + 1]
    open_braces = truncated.count("{") - truncated.count("}")
    if open_braces > 0:
        truncated = truncated.rstrip().rstrip(",")
        truncated += "}" * open_braces
    try:
        result = json.loads(truncated)
        if isinstance(result, dict):
            tqdm.write(f"  WARN: Repaired truncated JSON ({len(text)} chars)")
            return result
    except json.JSONDecodeError:
        pass

    # Attempt 4: salvage key-value pairs with regex
    salvaged = {}
    for m in re.finditer(r'"([^"]+)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[,}]', cleaned):
        salvaged[m.group(1)] = m.group(2).replace('\\"', '"').replace("\\n", "\n")
    if salvaged:
        tqdm.write(f"  WARN: Salvaged {len(salvaged)} entries from malformed JSON")
        return salvaged

    tqdm.write(f"  WARN: Could not parse Claude JSON ({len(text)} chars)")
    return {}


# ── Core: shorten + translate ─────────────────────────────────────────

def shorten_extracts(
    items: list[tuple[str, str, str]],
    target_words: int = 150,
    max_tokens: int = 16384,
) -> dict[str, dict[str, str]]:
    """Shorten excessively long extracts via Claude.

    items: [(scientific_name, locale, extract_text)]
    Returns: {scientific_name: {locale: shortened_text}}
    """
    if not items:
        return {}

    system_prompt = (
        "You are a concise natural history writer.\n\n"
        "TASK: Shorten each extract to ~{target} words while preserving "
        "key facts: appearance, habitat, geographic range, and behaviour.\n\n"
        "RULES:\n"
        "- Keep the SAME language as the input\n"
        "- Single flowing paragraph, no bullet points, no markdown\n"
        "- Do not start with the species name\n"
        "- Do not add information not present in the source\n\n"
        "OUTPUT FORMAT — return ONLY valid JSON, no markdown fences:\n"
        "{\n"
        '  "Scientific name": {"locale": "shortened text..."},\n'
        "  ...\n"
        "}"
    ).replace("{target}", str(target_words))

    entries = []
    for sci, loc, text in items:
        entries.append(f"- {sci} [{loc}]: {text}")

    user_message = (
        f"Shorten these {len(items)} extracts to ~{target_words} words each.\n\n"
        + "\n\n".join(entries)
    )

    result = _call_claude(system_prompt, user_message, max_tokens=max_tokens)
    if not result:
        return {}

    parsed = _parse_json_response(result)
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
    target_locales: locale codes to translate into (excluding 'en')
    Returns: {scientific_name: {locale: translation}}
    """
    locales = [l for l in target_locales if l != "en" and l in LOCALE_NAMES]
    if not locales or not items:
        return {}

    lang_list = ", ".join(f"{l} ({LOCALE_NAMES[l]})" for l in locales)

    system_prompt = (
        "You are a professional translator of natural history texts.\n\n"
        "TASK: Translate each species description to all requested languages.\n\n"
        "RULES:\n"
        "- Preserve meaning, tone, and approximate length\n"
        "- Use natural phrasing in each language\n"
        "- Single flowing paragraph, no bullet points, no markdown\n\n"
        "OUTPUT FORMAT — return ONLY valid JSON, no markdown fences:\n"
        "{\n"
        '  "Scientific name": {\n'
        '    "de": "German translation...",\n'
        '    "fr": "French translation...",\n'
        "    ...\n"
        "  },\n"
        "  ...\n"
        "}"
    )

    entries = []
    for sci, text in items:
        entries.append(f"- {sci}: {_truncate(text, max_source_chars)}")

    user_message = (
        f"Translate these {len(items)} species descriptions.\n"
        f"Languages: {lang_list}\n\n"
        + "\n\n".join(entries)
    )

    result = _call_claude(system_prompt, user_message, max_tokens=max_tokens)
    if not result:
        return {}

    parsed = _parse_json_response(result)
    out: dict[str, dict[str, str]] = {}
    locale_set = set(locales)
    for sci, trans in parsed.items():
        if isinstance(trans, dict):
            out[sci] = {
                k: v for k, v in trans.items()
                if k in locale_set and isinstance(v, str) and v.strip()
            }
    return out


# ── Pipeline ──────────────────────────────────────────────────────────

def _word_count(text: str) -> int:
    """Count words in text (handles CJK by counting characters)."""
    return len(text.split())


def _batch_by_char_budget(items: list, text_getter, char_budget: int,
                          max_items: int) -> list[list]:
    """Split items into batches bounded by char budget and item count."""
    batches: list[list] = []
    batch: list = []
    used_chars = 0
    limit = max(1, char_budget)
    max_batch_items = max(1, max_items)

    for item in items:
        text_len = max(1, len(text_getter(item)))
        if batch and (len(batch) >= max_batch_items or used_chars + text_len > limit):
            batches.append(batch)
            batch = []
            used_chars = 0
        batch.append(item)
        used_chars += text_len

    if batch:
        batches.append(batch)
    return batches


def _missing_translation_locales(
    wiki_extracts: dict[str, str],
    claude_extracts: dict[str, str],
    target_locales: list[str],
) -> tuple[str, ...]:
    """Return the exact set of locales still missing for one species."""
    return tuple(
        loc for loc in target_locales
        if not wiki_extracts.get(loc) and not claude_extracts.get(loc)
    )


def _build_translation_batches(
    items: list[tuple[str, str, tuple[str, ...]]],
    max_batch_size: int,
    char_budget: int,
    max_source_chars: int,
) -> list[tuple[tuple[str, ...], list[tuple[str, str]]]]:
    """Group translation work by exact locale signature, then char-budget batch."""
    grouped: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for sci, en_text, missing_locales in items:
        grouped.setdefault(missing_locales, []).append((sci, en_text))

    batches: list[tuple[tuple[str, ...], list[tuple[str, str]]]] = []
    sort_key = lambda item: (-len(item[1]), -len(item[0]), item[0])
    for missing_locales, grouped_items in sorted(grouped.items(), key=sort_key):
        grouped_batches = _batch_by_char_budget(
            grouped_items,
            text_getter=lambda item: _truncate(item[1], max_source_chars),
            char_budget=char_budget,
            max_items=max_batch_size,
        )
        for batch in grouped_batches:
            batches.append((missing_locales, batch))
    return batches


def _save_if_needed(existing: dict, batches_since_save: int, save_every: int) -> int:
    """Checkpoint Claude output periodically instead of after every batch."""
    if batches_since_save >= save_every:
        _checkpoint_state(existing)
        return 0
    return batches_since_save


def _merge_updates(target: dict, updates: dict[str, dict[str, str]]):
    """Merge batch updates into the in-memory Claude output structure."""
    for sci, extracts in updates.items():
        entry = target.setdefault(sci, {})
        entry_extracts = entry.setdefault("extracts", {})
        entry_extracts.update(extracts)


def _append_journal(updates: dict[str, dict[str, str]]):
    """Persist one completed batch to an append-only journal for resume safety."""
    if not updates:
        return
    with JOURNAL_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(updates, ensure_ascii=False) + "\n")


def _load_existing_outputs() -> dict:
    """Load the main Claude output file and replay any unsnapshotted journal."""
    existing = load_json(OUTPUT_FILE)
    if not JOURNAL_FILE.exists():
        return existing

    replayed = 0
    with JOURNAL_FILE.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                updates = json.loads(line)
            except json.JSONDecodeError:
                tqdm.write("  WARN: Skipping malformed Claude journal line")
                continue
            if isinstance(updates, dict):
                _merge_updates(existing, updates)
                replayed += 1
    if replayed:
        tqdm.write(f"  Replayed {replayed} Claude journal batches")
    return existing


def _checkpoint_state(existing: dict):
    """Write the full Claude output and clear the batch journal."""
    save_json(existing, OUTPUT_FILE)
    if JOURNAL_FILE.exists():
        JOURNAL_FILE.unlink()


def main():
    cfg = load_config()
    claude_cfg = cfg.get("claude", {})
    delay = claude_cfg.get("request_delay", 0.5)
    default_batch = claude_cfg.get("batch_size", 5)
    max_tokens = claude_cfg.get("max_tokens", 16384)
    max_words = claude_cfg.get("max_extract_words", 500)
    target_words = claude_cfg.get("target_words", 150)
    target_locales = claude_cfg.get("locales", ["en"])
    translate_workers = claude_cfg.get("translate_workers", 4)
    save_every = claude_cfg.get("save_every", 10)
    source_char_budget = claude_cfg.get("source_char_budget", 12000)
    max_source_chars = claude_cfg.get("max_source_chars", 3000)
    target_non_en = [l for l in target_locales if l != "en"]

    parser = argparse.ArgumentParser(
        description="Translate and shorten species descriptions via Claude"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed without calling API")
    parser.add_argument("--shorten-only", action="store_true",
                        help="Only shorten long extracts, skip translations")
    parser.add_argument("--translate-only", action="store_true",
                        help="Only translate, skip shortening")
    parser.add_argument("--batch-size", type=int, default=default_batch,
                        help=f"Species per API call (default: {default_batch})")
    parser.add_argument("--workers", type=int, default=translate_workers,
                        help=f"Parallel translation workers (default: {translate_workers})")
    parser.add_argument("--save-every", type=int, default=save_every,
                        help=f"Save claude_data.json every N completed batches (default: {save_every})")
    parser.add_argument("--char-budget", type=int, default=source_char_budget,
                        help=f"Approximate source-character budget per API call (default: {source_char_budget})")
    parser.add_argument("--max-source-chars", type=int, default=max_source_chars,
                        help=f"Max source chars per species sent to Claude (default: {max_source_chars})")
    args = parser.parse_args()

    global _request_delay
    _request_delay = max(0.0, float(delay))

    # Load source data
    wiki = load_json(WIKI_DATA)
    if not wiki:
        print("ERROR: wikipedia_data.json not found. "
              "Run collectors/wikipedia.py first.")
        raise SystemExit(1)

    existing = _load_existing_outputs()
    print(f"Loaded {len(wiki)} species from Wikipedia")
    print(f"Already have Claude data for {len(existing)} species")
    print(f"Target locales: {', '.join(target_locales)}")
    print(f"Shorten threshold: >{max_words} words → ~{target_words} words")

    # ── Phase 1: Find extracts that need shortening ───────────────────
    needs_shortening: list[tuple[str, str, str]] = []  # (sci, locale, text)
    if not args.translate_only:
        for sci, rec in wiki.items():
            for loc, text in rec.get("extracts", {}).items():
                if not text:
                    continue
                # Skip if already shortened by Claude
                cl_entry = existing.get(sci, {})
                if cl_entry.get("extracts", {}).get(loc):
                    continue
                if _word_count(text) > max_words:
                    needs_shortening.append((sci, loc, text))

    # ── Phase 2: Find species needing translation ─────────────────────
    needs_translation: list[tuple[str, str, tuple[str, ...]]] = []
    if not args.shorten_only and target_non_en:
        for sci, rec in wiki.items():
            en_text = rec.get("extract") or rec.get("extracts", {}).get("en")
            if not en_text:
                continue
            # Check which target locales are missing
            wp_extracts = rec.get("extracts", {})
            cl_extracts = existing.get(sci, {}).get("extracts", {})
            missing = _missing_translation_locales(
                wp_extracts, cl_extracts, target_non_en,
            )
            if missing:
                needs_translation.append((sci, en_text, missing))

    if args.limit:
        needs_shortening = needs_shortening[:args.limit]
        remaining = max(0, args.limit - len(needs_shortening))
        needs_translation = needs_translation[:remaining]

    batch_size = max(1, args.batch_size)
    save_every = max(1, args.save_every)
    char_budget = max(1, args.char_budget)
    max_source_chars = max(500, args.max_source_chars)

    shorten_batches = _batch_by_char_budget(
        needs_shortening,
        text_getter=lambda item: item[2],
        char_budget=char_budget,
        max_items=batch_size,
    )
    translation_batches = _build_translation_batches(
        needs_translation,
        max_batch_size=batch_size,
        char_budget=char_budget,
        max_source_chars=max_source_chars,
    )
    locale_signatures = {missing for _, _, missing in needs_translation}

    print(f"\n  Phase 1 — Shorten: {len(needs_shortening)} extracts "
          f"({len(shorten_batches)} batches)")
    print(f"  Phase 2 — Translate: {len(needs_translation)} species "
          f"({len(translation_batches)} batches across "
          f"{len(locale_signatures)} locale sets)")
    print(f"  Estimated API calls: ~{len(shorten_batches) + len(translation_batches)}")

    if args.dry_run:
        if needs_shortening:
            print(f"\n  Extracts to shorten (>{max_words} words):")
            for sci, loc, text in needs_shortening[:20]:
                wc = _word_count(text)
                print(f"    {sci} [{loc}]: {wc} words")
            if len(needs_shortening) > 20:
                print(f"    ... and {len(needs_shortening) - 20} more")
        if needs_translation:
            print(f"\n  Species to translate (first 20):")
            for sci, text, missing in needs_translation[:20]:
                wp_ext = wiki[sci].get("extracts", {})
                cl_ext = existing.get(sci, {}).get("extracts", {})
                have = sorted(set(wp_ext) | set(cl_ext))
                print(f"    {sci}: have={','.join(have)}, "
                      f"missing={','.join(missing)}")
            if len(needs_translation) > 20:
                print(f"    ... and {len(needs_translation) - 20} more")
        return

    # ── Phase 1: Shorten ──────────────────────────────────────────────
    if needs_shortening:
        print(f"\nPhase 1: Shortening {len(needs_shortening)} extracts...")
        pbar = tqdm(total=len(needs_shortening), desc="Shorten", unit="ext")
        pending_saves = 0

        for batch in shorten_batches:
            if is_shutting_down():
                break
            results = shorten_extracts(
                batch, target_words=target_words, max_tokens=max_tokens,
            )
            batch_updates: dict[str, dict[str, str]] = {}

            for sci, locales in results.items():
                for loc, text in locales.items():
                    batch_updates.setdefault(sci, {})[loc] = text

            _merge_updates(existing, batch_updates)
            _append_journal(batch_updates)

            pbar.update(len(batch))
            pending_saves += 1
            pending_saves = _save_if_needed(existing, pending_saves, save_every)

        pbar.close()
        if pending_saves:
            _checkpoint_state(existing)
        shortened = sum(
            len(v.get("extracts", {})) for v in existing.values()
        )
        print(f"  Total extracts in Claude data: {shortened}")
    else:
        print("\nPhase 1: No extracts need shortening")

    if is_shutting_down():
        return

    # ── Phase 2: Translate ────────────────────────────────────────────
    if needs_translation:
        print(f"\nPhase 2: Translating {len(needs_translation)} species...")
        pbar = tqdm(total=len(needs_translation), desc="Translate", unit="sp")
        pending_saves = 0

        def _translate_batch(missing_locales: tuple[str, ...], batch: list[tuple[str, str]]):
            results = translate_extracts(
                batch,
                list(missing_locales),
                max_source_chars=max_source_chars,
                max_tokens=max_tokens,
            )
            return missing_locales, batch, results

        max_workers = max(1, args.workers)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_translate_batch, missing_locales, batch): (missing_locales, batch)
                for missing_locales, batch in translation_batches
            }
            for future in as_completed(futures):
                if is_shutting_down():
                    break
                missing_locales, batch = futures[future]
                try:
                    _, _, results = future.result()
                except Exception as exc:
                    tqdm.write(
                        f"  Claude translation batch failed for locales "
                        f"{','.join(missing_locales)}: {exc}"
                    )
                    pbar.update(len(batch))
                    continue

                batch_updates: dict[str, dict[str, str]] = {}

                for sci, trans in results.items():
                    wp_ext = wiki.get(sci, {}).get("extracts", {})
                    for loc, text in trans.items():
                        current = existing.get(sci, {}).get("extracts", {})
                        if not wp_ext.get(loc) and not current.get(loc):
                            batch_updates.setdefault(sci, {})[loc] = text

                _merge_updates(existing, batch_updates)
                _append_journal(batch_updates)

                pbar.update(len(batch))
                pending_saves += 1
                pending_saves = _save_if_needed(existing, pending_saves, save_every)

        pbar.close()
        if pending_saves:
            _checkpoint_state(existing)
    else:
        print("\nPhase 2: No translations needed")

    # Summary
    n_species = len(existing)
    n_extracts = sum(len(v.get("extracts", {})) for v in existing.values())
    print(f"\nDone! {n_species} species, {n_extracts} total extracts "
          f"in {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
