#!/usr/bin/env python3
"""
Generate and translate species descriptions using the Claude API.

Step 5 in the pipeline. Reads source text from ebird_data.json and
wikipedia_data.json, generates ~100-word English descriptions for batches
of species, then batch-translates to all configured locales.

Batching: multiple species per API call to minimise request count.
Source text is truncated to max_source_chars (config) to save input tokens.

Output: raw_data/claude_data.json (incremental, resumable)

Usage:
    python -m utils.claude [--limit N] [--dry-run] [--batch-size N]

Requires ANTHROPIC_API_KEY in .env file.
"""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from utils.config import load_config, get_locales, LOCALE_NAMES

# Paths
ROOT = Path(__file__).resolve().parent.parent
INAT_DATA = ROOT / "raw_data" / "inat_data.json"
EBIRD_DATA = ROOT / "raw_data" / "ebird_data.json"
WIKI_DATA = ROOT / "raw_data" / "wikipedia_data.json"
OUTPUT_FILE = ROOT / "raw_data" / "claude_data.json"

_api_key = None
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"


def _get_claude_config() -> dict:
    """Load Claude settings from config.yml."""
    return load_config().get("claude", {})


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


def _call_claude(system_prompt: str, user_message: str, max_tokens: int = 1024) -> str | None:
    """Make a request to the Claude API."""
    api_key = _load_api_key()

    model = _get_claude_config().get("model", "claude-sonnet-4-20250514")
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

    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"  Claude API error {e.code}: {body[:200]}")
        return None
    except (URLError, TimeoutError) as e:
        print(f"  Claude API error: {e}")
        return None

    # Extract text from response
    content = data.get("content", [])
    if content and content[0].get("type") == "text":
        return content[0]["text"].strip()
    return None


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, breaking at a sentence boundary if possible."""
    if not text or len(text) <= max_chars:
        return text
    # Try to break at last sentence end within limit
    truncated = text[:max_chars]
    for sep in (". ", ".\n", "! ", "? "):
        idx = truncated.rfind(sep)
        if idx > max_chars // 2:
            return truncated[:idx + 1]
    return truncated.rsplit(" ", 1)[0] + "…"


def generate_description(
    scientific_name: str,
    english_name: str,
    ebird_description: str = "",
    wikipedia_summary: str = "",
) -> str | None:
    """Generate a ~100-word species description using Claude (single species)."""
    claude_cfg = _get_claude_config()
    max_tokens = claude_cfg.get("description_max_tokens", 2048)
    max_src = claude_cfg.get("max_source_chars", 500)

    system_prompt = (
        "You are a concise natural history writer. Write exactly around 100 words. "
        "Do not use markdown, bullet points, or headers. Write a single flowing paragraph. "
        "Do not start with the species name."
    )

    source_text = ""
    if ebird_description:
        source_text += f"eBird description:\n{_truncate(ebird_description, max_src)}\n\n"
    if wikipedia_summary:
        source_text += f"Wikipedia summary:\n{_truncate(wikipedia_summary, max_src)}\n\n"

    if not source_text.strip():
        return None

    user_message = (
        f"Write a ~100-word description of {english_name} ({scientific_name}). "
        f"Cover these four aspects: (1) appearance/looks, (2) habitat, (3) geographic range, "
        f"and (4) one interesting fact. Use the following source material:\n\n"
        f"{source_text}"
        f"Write a single concise paragraph, approximately 100 words."
    )

    return _call_claude(system_prompt, user_message, max_tokens=max_tokens)


def generate_descriptions_batch(
    species_list: list[tuple[str, str, str, str]],
) -> dict[str, str]:
    """Generate ~100-word descriptions for multiple species in one API call.

    species_list: list of (scientific_name, english_name, ebird_desc, wiki_extract)
    Returns: {scientific_name: description}
    """
    claude_cfg = _get_claude_config()
    max_tokens = claude_cfg.get("description_max_tokens", 2048)
    max_src = claude_cfg.get("max_source_chars", 500)

    system_prompt = (
        "You are a concise natural history writer. For each species below, "
        "write exactly around 100 words covering: appearance, habitat, geographic range, "
        "and one interesting fact. Write a single flowing paragraph per species. "
        "Do not start with the species name. Do not use markdown.\n\n"
        "Return ONLY valid JSON: {\"Scientific name\": \"description\", ...}\n"
        "No markdown, no code fences, no extra text."
    )

    entries = []
    for sci, en, eb, wi in species_list:
        source = ""
        if eb:
            source += f"eBird: {_truncate(eb, max_src)} "
        if wi:
            source += f"Wikipedia: {_truncate(wi, max_src)}"
        entries.append(f"- {en} ({sci}): {source.strip()}")

    user_message = (
        f"Write ~100-word descriptions for these {len(species_list)} species:\n\n"
        + "\n".join(entries)
        + "\n\nReturn JSON: {\"Scientific name\": \"description\", ...}"
    )

    result = _call_claude(system_prompt, user_message, max_tokens=max_tokens)
    return _parse_json_response(result) if result else {}


def translate_description(
    description: str,
    target_locale: str,
    species_name: str = "",
) -> str | None:
    """Translate a species description to a target locale using Claude."""
    lang = LOCALE_NAMES.get(target_locale, target_locale)

    system_prompt = (
        f"You are a professional translator specializing in natural history texts. "
        f"Translate to {lang}. Preserve the meaning, tone, and approximate length. "
        f"Output only the translated text, nothing else."
    )

    user_message = (
        f"Translate this species description to {lang}:\n\n{description}"
    )

    return _call_claude(system_prompt, user_message, max_tokens=500)


def translate_batch(
    description: str,
    target_locales: list[str],
    species_name: str = "",
) -> dict[str, str]:
    """Translate one description to multiple locales in a single API call."""
    locales_to_translate = [l for l in target_locales if l != "en" and l in LOCALE_NAMES]
    if not locales_to_translate:
        return {}

    claude_cfg = _get_claude_config()
    max_tokens = claude_cfg.get("translation_max_tokens", 8192)

    lang_list = ", ".join(f"{l} ({LOCALE_NAMES[l]})" for l in locales_to_translate)

    system_prompt = (
        "You are a professional translator specializing in natural history texts. "
        "Translate the given text to each requested language. "
        "Preserve the meaning, tone, and approximate length. "
        "Return ONLY valid JSON with locale codes as keys and translations as values. "
        "No markdown, no code fences, no extra text."
    )

    user_message = (
        f"Translate this species description to these languages: {lang_list}\n\n"
        f"Text:\n{description}\n\n"
        f"Return JSON like: {{\"de\": \"...\", \"fr\": \"...\", ...}}"
    )

    result = _call_claude(system_prompt, user_message, max_tokens=max_tokens)
    if not result:
        return {}

    parsed = _parse_json_response(result)
    return {k: v for k, v in parsed.items() if k in locales_to_translate}


def translate_batch_multi(
    descriptions: dict[str, str],
    target_locales: list[str],
) -> dict[str, dict[str, str]]:
    """Translate multiple species descriptions to all target locales in one call.

    descriptions: {scientific_name: english_description}
    Returns: {scientific_name: {locale: translation}}
    """
    locales_to_translate = [l for l in target_locales if l != "en" and l in LOCALE_NAMES]
    if not locales_to_translate or not descriptions:
        return {}

    claude_cfg = _get_claude_config()
    max_tokens = claude_cfg.get("translation_max_tokens", 8192)
    lang_list = ", ".join(f"{l} ({LOCALE_NAMES[l]})" for l in locales_to_translate)

    system_prompt = (
        "You are a professional translator specializing in natural history texts. "
        "Translate each species description to all requested languages. "
        "Preserve the meaning, tone, and approximate length.\n\n"
        "Return ONLY valid JSON:\n"
        "{\"Scientific name\": {\"de\": \"...\", \"fr\": \"...\", ...}, ...}\n"
        "No markdown, no code fences, no extra text."
    )

    entries = []
    for sci, desc in descriptions.items():
        entries.append(f"- {sci}: {desc}")

    user_message = (
        f"Translate these {len(descriptions)} species descriptions "
        f"to these languages: {lang_list}\n\n"
        + "\n".join(entries)
        + f"\n\nReturn JSON: {{\"Scientific name\": {{\"de\": \"...\", ...}}, ...}}"
    )

    result = _call_claude(system_prompt, user_message, max_tokens=max_tokens)
    if not result:
        return {}

    parsed = _parse_json_response(result)
    # Validate structure: {str: {str: str}}
    out = {}
    for sci, trans in parsed.items():
        if isinstance(trans, dict):
            out[sci] = {k: v for k, v in trans.items() if k in locales_to_translate}
    return out


def _parse_json_response(text: str) -> dict:
    """Parse a JSON response from Claude, handling code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        print(f"  WARN: Could not parse Claude JSON response ({len(text)} chars)")
    return {}


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_existing_data() -> dict:
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    cfg = load_config()
    locales = get_locales()
    claude_cfg = cfg.get("claude", {})
    delay = claude_cfg.get("request_delay", 0.5)
    default_batch = claude_cfg.get("batch_size", 5)

    parser = argparse.ArgumentParser(
        description="Generate and translate species descriptions via Claude"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed without calling API")
    parser.add_argument("--save-every", type=int, default=10,
                        help="Save progress every N species")
    parser.add_argument("--skip-translate", action="store_true",
                        help="Only generate English descriptions, skip translations")
    parser.add_argument("--batch-size", type=int, default=default_batch,
                        help=f"Species per API call (default: {default_batch})")
    args = parser.parse_args()

    # Load source data
    inat = _load_json(INAT_DATA)
    ebird = _load_json(EBIRD_DATA)
    wiki = _load_json(WIKI_DATA)

    if not inat:
        print("ERROR: inat_data.json not found. Run utils/inat.py first.")
        raise SystemExit(1)

    existing = load_existing_data()
    print(f"Loaded {len(inat)} species from iNat, {len(ebird)} from eBird, {len(wiki)} from Wikipedia")
    print(f"Already have Claude data for {len(existing)} species")
    print(f"Target locales: {', '.join(locales)}")

    # Build list of species to process: need source text and not yet done
    to_process = []
    for sci_name in inat:
        if sci_name in existing and existing[sci_name].get("description_en"):
            continue
        ebird_desc = ebird.get(sci_name, {}).get("description", "") or ""
        wiki_extract = wiki.get(sci_name, {}).get("extract", "") or ""
        if not ebird_desc and not wiki_extract:
            continue  # no source text available
        english_name = inat[sci_name].get("preferred_common_name", sci_name)
        to_process.append((sci_name, english_name, ebird_desc, wiki_extract))

    if args.limit:
        to_process = to_process[:args.limit]

    batch_size = max(1, args.batch_size)
    n_batches = (len(to_process) + batch_size - 1) // batch_size
    api_calls = n_batches * (1 if args.skip_translate else 2)

    print(f"Will process {len(to_process)} species in {n_batches} batches of {batch_size}")
    print(f"Estimated API calls: {api_calls} (vs {len(to_process) * 2} without batching)")

    if args.dry_run:
        for sci, en, eb, wi in to_process[:20]:
            sources = []
            if eb:
                sources.append("ebird")
            if wi:
                sources.append("wiki")
            print(f"  {sci} ({en}) — sources: {', '.join(sources)}")
        if len(to_process) > 20:
            print(f"  ... and {len(to_process) - 20} more")
        return

    processed = 0
    for batch_idx in range(n_batches):
        batch_start = batch_idx * batch_size
        batch = to_process[batch_start:batch_start + batch_size]
        sci_names = [s[0] for s in batch]

        print(f"\n  Batch {batch_idx + 1}/{n_batches} "
              f"({len(batch)} species: {', '.join(s[1] for s in batch)})...",
              flush=True)

        # --- Step 1: Generate descriptions ---
        if len(batch) == 1:
            # Single species — use simpler prompt
            sci, en, eb, wi = batch[0]
            desc = generate_description(sci, en, eb, wi)
            descs = {sci: desc} if desc else {}
        else:
            descs = generate_descriptions_batch(batch)

        time.sleep(delay)

        if not descs:
            print(f"    WARN: No descriptions returned for this batch")
            for sci, _, _, _ in batch:
                existing[sci] = {"description_en": None, "error": "no_response"}
            processed += len(batch)
            continue

        for sci, en, _, _ in batch:
            desc = descs.get(sci, "")
            if desc:
                print(f"    ✓ {sci}: {len(desc)} chars")
            else:
                print(f"    ✗ {sci}: no description")

        # --- Step 2: Translate ---
        translations_all = {}
        if not args.skip_translate and descs:
            if len(descs) == 1:
                sci = next(iter(descs))
                trans = translate_batch(descs[sci], locales)
                translations_all = {sci: trans} if trans else {}
            else:
                translations_all = translate_batch_multi(descs, locales)
            time.sleep(delay)

            n_translated = sum(1 for t in translations_all.values() if t)
            n_locales = sum(len(t) for t in translations_all.values())
            print(f"    Translated {n_translated} species → {n_locales} total locale entries")

        # --- Save records ---
        for sci, en, _, _ in batch:
            desc_en = descs.get(sci, "")
            record = {"description_en": desc_en or None}
            if not desc_en:
                record["error"] = "no_response"
            if sci in translations_all:
                record["translations"] = translations_all[sci]
            existing[sci] = record
            processed += 1

        if processed % args.save_every < batch_size:
            save_data(existing)
            print(f"  --- Saved progress ({processed}/{len(to_process)} processed) ---")

    save_data(existing)
    with_desc = sum(1 for v in existing.values() if v.get("description_en"))
    print(f"\nDone! Processed {processed} species in {n_batches} batches.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries ({with_desc} with descriptions)")


if __name__ == "__main__":
    main()