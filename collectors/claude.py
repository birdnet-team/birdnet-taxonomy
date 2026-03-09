#!/usr/bin/env python3
"""
Generate and translate species descriptions using the Claude API.

Reads source text from ebird_data.json and wikipedia_data.json, then for
each batch of species makes a **single** API call that both generates
~100-word English descriptions and translates them to all configured
locales.

Source text is truncated to max_source_chars (config) to save input tokens.

Output: raw_data/claude_data.json (incremental, resumable)

Usage:
    python -m collectors.claude [--limit N] [--dry-run] [--batch-size N]

Requires ANTHROPIC_API_KEY in .env file.
"""

import argparse
import json
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from tqdm import tqdm

from config import load_config, get_locales, LOCALE_NAMES
from collectors._common import (
    ROOT, RAW_DIR, setup_shutdown, is_shutting_down,
    load_json, save_json,
)

setup_shutdown()

INAT_DATA = RAW_DIR / "inat_data.json"
EBIRD_DATA = RAW_DIR / "ebird_data.json"
WIKI_DATA = RAW_DIR / "wikipedia_data.json"
OUTPUT_FILE = RAW_DIR / "claude_data.json"

_api_key = None
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"


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

    for attempt in range(3):
        try:
            with urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("content", [])
                if content and content[0].get("type") == "text":
                    return content[0]["text"].strip()
                return None
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            if e.code == 429 or e.code >= 500:
                wait = min(2 ** (attempt + 1), 30)
                tqdm.write(f"  Claude {e.code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            tqdm.write(f"  Claude API error {e.code}: {body[:200]}")
            return None
        except (URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(min(2 ** attempt, 10))
                continue
            tqdm.write(f"  Claude API error: {e}")
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


# ── Core: describe + translate in one call ────────────────────────────

def describe_and_translate(
    species_list: list[tuple[str, str, str, str]],
    target_locales: list[str],
    max_source_chars: int = 500,
    max_tokens: int = 16384,
) -> dict[str, dict[str, str]]:
    """Generate descriptions AND translations in a single API call.

    species_list: [(scientific_name, english_name, ebird_desc, wiki_extract)]
    target_locales: list of locale codes including 'en'

    Returns: {scientific_name: {"en": "desc", "de": "...", "fr": "...", ...}}

    A single call handles both description generation and translation,
    halving the number of API requests compared to describe-then-translate.
    """
    non_en = [l for l in target_locales if l != "en" and l in LOCALE_NAMES]
    lang_list = ", ".join(f"{l} ({LOCALE_NAMES[l]})" for l in non_en)

    system_prompt = (
        "You are a concise natural history writer and professional translator.\n\n"
        "TASK: For each species, write a ~100-word English description, then "
        "translate it to all requested languages.\n\n"
        "DESCRIPTION RULES:\n"
        "- Exactly ~100 words, single flowing paragraph\n"
        "- Cover: appearance, habitat, geographic range, one interesting fact\n"
        "- Do not start with the species name\n"
        "- No markdown, no bullet points\n\n"
        "TRANSLATION RULES:\n"
        "- Preserve meaning, tone, and approximate length\n"
        "- Use natural phrasing in each language\n\n"
        "OUTPUT FORMAT — return ONLY valid JSON, no markdown fences:\n"
        "{\n"
        '  "Scientific name": {\n'
        '    "en": "English description...",\n'
        '    "de": "German translation...",\n'
        '    "fr": "French translation...",\n'
        "    ...\n"
        "  },\n"
        "  ...\n"
        "}"
    )

    entries = []
    for sci, en, eb, wi in species_list:
        source = ""
        if eb:
            source += f"eBird: {_truncate(eb, max_source_chars)} "
        if wi:
            source += f"Wikipedia: {_truncate(wi, max_source_chars)}"
        entries.append(f"- {en} ({sci}): {source.strip()}")

    user_message = (
        f"Describe and translate these {len(species_list)} species.\n"
        f"Languages: en (English), {lang_list}\n\n"
        + "\n".join(entries)
    )

    result = _call_claude(system_prompt, user_message, max_tokens=max_tokens)
    if not result:
        return {}

    parsed = _parse_json_response(result)

    # Validate structure: {str: {str: str}}
    out = {}
    valid_locales = {"en"} | set(non_en)
    for sci, translations in parsed.items():
        if isinstance(translations, dict):
            out[sci] = {
                k: v for k, v in translations.items()
                if k in valid_locales and isinstance(v, str) and v.strip()
            }

    return out


def translate_missing(
    descriptions: dict[str, str],
    missing_locales: list[str],
    max_tokens: int = 8192,
) -> dict[str, dict[str, str]]:
    """Translate existing descriptions to specific missing locales only.

    Used to fill gaps when some locales failed in the initial call.

    descriptions: {scientific_name: english_description}
    missing_locales: locale codes to translate to

    Returns: {scientific_name: {locale: translation}}
    """
    locales = [l for l in missing_locales if l in LOCALE_NAMES]
    if not locales or not descriptions:
        return {}

    lang_list = ", ".join(f"{l} ({LOCALE_NAMES[l]})" for l in locales)

    system_prompt = (
        "You are a professional translator of natural history texts. "
        "Translate each description to all requested languages. "
        "Preserve meaning, tone, and length.\n\n"
        "Return ONLY valid JSON:\n"
        '{"Scientific name": {"locale": "translation", ...}, ...}\n'
        "No markdown, no code fences."
    )

    entries = [f"- {sci}: {desc}" for sci, desc in descriptions.items()]
    user_message = (
        f"Translate to: {lang_list}\n\n"
        + "\n".join(entries)
    )

    result = _call_claude(system_prompt, user_message, max_tokens=max_tokens)
    if not result:
        return {}

    parsed = _parse_json_response(result)
    out = {}
    locale_set = set(locales)
    for sci, trans in parsed.items():
        if isinstance(trans, dict):
            out[sci] = {k: v for k, v in trans.items()
                        if k in locale_set and isinstance(v, str) and v.strip()}
    return out


# ── Pipeline ──────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    locales = get_locales()
    claude_cfg = cfg.get("claude", {})
    delay = claude_cfg.get("request_delay", 0.5)
    default_batch = claude_cfg.get("batch_size", 10)
    max_src = claude_cfg.get("max_source_chars", 500)
    max_tokens = claude_cfg.get("max_tokens", 16384)

    parser = argparse.ArgumentParser(
        description="Generate and translate species descriptions via Claude"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed without calling API")
    parser.add_argument("--skip-translate", action="store_true",
                        help="Only generate English descriptions, skip translations")
    parser.add_argument("--batch-size", type=int, default=default_batch,
                        help=f"Species per API call (default: {default_batch})")
    args = parser.parse_args()

    # Load source data
    inat = load_json(INAT_DATA)
    ebird = load_json(EBIRD_DATA)
    wiki = load_json(WIKI_DATA)

    if not inat:
        print("ERROR: inat_data.json not found. Run collectors/inat.py first.")
        raise SystemExit(1)

    existing = load_json(OUTPUT_FILE)
    print(f"Loaded {len(inat)} species from iNat, "
          f"{len(ebird)} from eBird, {len(wiki)} from Wikipedia")
    print(f"Already have Claude data for {len(existing)} species")

    target_non_en = [l for l in locales if l != "en"]
    n_target = len(target_non_en)
    # For --skip-translate, only use 'en'
    effective_locales = ["en"] if args.skip_translate else locales

    print(f"Target locales: {', '.join(effective_locales)}")

    # Build work list — categorize species by what they need
    needs_everything = []    # no entry or no description
    needs_translation = []   # has description but missing translations

    for sci_name in inat:
        ebird_desc = ebird.get(sci_name, {}).get("description", "") or ""
        wiki_extract = wiki.get(sci_name, {}).get("extract", "") or ""
        if not ebird_desc and not wiki_extract:
            continue  # no source text

        english_name = inat[sci_name].get("preferred_common_name", sci_name)
        obs_count = inat[sci_name].get("observations_count", 0) or 0
        item = (sci_name, english_name, ebird_desc, wiki_extract, obs_count)

        entry = existing.get(sci_name)
        if not entry or not entry.get("description_en"):
            needs_everything.append(item)
        elif not args.skip_translate:
            trans = entry.get("translations", {})
            if len(trans) < n_target:
                needs_translation.append(item)

    # Sort by observation count (most common first) so we can abort
    # at any point and still have the most important species done
    needs_everything.sort(key=lambda x: x[4], reverse=True)
    needs_translation.sort(key=lambda x: x[4], reverse=True)

    print(f"  New/retry: {len(needs_everything)}, "
          f"partial translations: {len(needs_translation)}")

    # Process in order: new/retry first, then partial translations
    to_process = needs_everything + needs_translation
    if args.limit:
        to_process = to_process[:args.limit]

    batch_size = max(1, args.batch_size)
    n_batches = (len(to_process) + batch_size - 1) // batch_size

    print(f"Will process {len(to_process)} species in {n_batches} batches of {batch_size}")
    print(f"Estimated API calls: ~{n_batches} "
          f"(single call per batch: describe + translate)")

    if args.dry_run:
        for sci, en, eb, wi, obs in to_process[:20]:
            sources = []
            if eb:
                sources.append("ebird")
            if wi:
                sources.append("wiki")
            status = "new" if sci not in existing else "retry/partial"
            obs_str = f"{obs:,}" if obs else "?"
            print(f"  {sci} ({en}) — {status}, {obs_str} obs, sources: {', '.join(sources)}")
        if len(to_process) > 20:
            print(f"  ... and {len(to_process) - 20} more")
        return

    processed = 0
    pbar = tqdm(total=len(to_process), desc="Claude", unit="sp")

    for batch_idx in range(n_batches):
        if is_shutting_down():
            break

        batch_start = batch_idx * batch_size
        batch = to_process[batch_start:batch_start + batch_size]

        pbar.set_postfix_str(
            f"batch {batch_idx + 1}/{n_batches}", refresh=False
        )

        # Strip obs_count from tuples (only used for sorting)
        batch_4 = [(s, e, eb, wi) for s, e, eb, wi, _ in batch]

        # Split: species needing full describe+translate vs translation-only
        need_desc = [(s, e, eb, wi) for s, e, eb, wi in batch_4
                     if not existing.get(s, {}).get("description_en")]
        have_desc = [(s, e, eb, wi) for s, e, eb, wi in batch_4
                     if existing.get(s, {}).get("description_en")]

        # ── Full describe + translate for new species ──
        results = {}
        if need_desc:
            results = describe_and_translate(
                need_desc, effective_locales,
                max_source_chars=max_src, max_tokens=max_tokens,
            )
            time.sleep(delay)

        # ── Translation-only for species with existing descriptions ──
        if have_desc and not args.skip_translate:
            descs_for_trans = {}
            missing_per_species: dict[str, list[str]] = {}

            for sci, en, _, _ in have_desc:
                entry = existing[sci]
                desc = entry["description_en"]
                existing_trans = set(entry.get("translations", {}).keys())
                missing = [l for l in target_non_en if l not in existing_trans]
                if missing:
                    descs_for_trans[sci] = desc
                    missing_per_species[sci] = missing

            if descs_for_trans:
                # Find the union of all missing locales
                all_missing = sorted(
                    set(l for locs in missing_per_species.values() for l in locs)
                )
                trans_results = translate_missing(
                    descs_for_trans, all_missing, max_tokens=max_tokens,
                )
                # Merge into results format
                for sci, trans in trans_results.items():
                    results[sci] = {"en": descs_for_trans[sci]}
                    results[sci].update(trans)

                time.sleep(delay)

        # ── Save records ──
        if not results and need_desc:
            tqdm.write(f"  WARN: No results for batch {batch_idx + 1}")
            for sci, _, _, _ in need_desc:
                existing[sci] = {"description_en": None, "error": "no_response"}

        for sci, en, _, _ in batch_4:
            species_result = results.get(sci, {})
            old = existing.get(sci, {})
            desc_en = species_result.get("en") or old.get("description_en")

            record = {"description_en": desc_en or None}
            if not desc_en and sci in [s for s, _, _, _ in need_desc]:
                record["error"] = "no_description"

            # Merge translations: keep old, overlay new
            merged_trans = dict(old.get("translations", {}))
            for locale, text in species_result.items():
                if locale != "en" and text:
                    merged_trans[locale] = text
            if merged_trans:
                record["translations"] = merged_trans

            existing[sci] = record
            processed += 1

        pbar.update(len(batch))
        save_json(existing, OUTPUT_FILE)

    pbar.close()

    with_desc = sum(1 for v in existing.values() if v.get("description_en"))
    with_full_trans = sum(
        1 for v in existing.values()
        if len(v.get("translations", {})) >= n_target
    )
    print(f"\nDone! Processed {processed} species in {n_batches} batches.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries "
          f"({with_desc} with descriptions, {with_full_trans} fully translated)")


if __name__ == "__main__":
    main()
