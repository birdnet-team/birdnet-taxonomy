#!/usr/bin/env python3
"""
Generate and translate species descriptions using the Claude API.

Step 5 in the pipeline. Reads source text from ebird_data.json and
wikipedia_data.json, generates a ~100-word English description for each
species, then batch-translates to all configured locales.

Output: raw_data/claude_data.json (incremental, resumable)

Usage:
    python -m utils.claude [--limit N] [--dry-run]

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


def generate_description(
    scientific_name: str,
    english_name: str,
    ebird_description: str = "",
    wikipedia_summary: str = "",
) -> str | None:
    """Generate a ~100-word species description using Claude.

    Combines eBird and Wikipedia source text to produce a concise description
    covering: appearance, habitat, range, and an interesting fact.
    """
    claude_cfg = _get_claude_config()
    max_tokens = claude_cfg.get("description_max_tokens", 300)

    system_prompt = (
        "You are a concise natural history writer. Write exactly around 100 words. "
        "Do not use markdown, bullet points, or headers. Write a single flowing paragraph. "
        "Do not start with the species name."
    )

    source_text = ""
    if ebird_description:
        source_text += f"eBird description:\n{ebird_description}\n\n"
    if wikipedia_summary:
        source_text += f"Wikipedia summary:\n{wikipedia_summary}\n\n"

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
    """Translate a description to multiple locales in a single API call."""
    locales_to_translate = [l for l in target_locales if l != "en" and l in LOCALE_NAMES]
    if not locales_to_translate:
        return {}

    claude_cfg = _get_claude_config()
    max_tokens = claude_cfg.get("translation_max_tokens", 4096)

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

    # Parse JSON response
    try:
        # Handle potential markdown code fences
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        translations = json.loads(cleaned)
        if isinstance(translations, dict):
            return {k: v for k, v in translations.items() if k in locales_to_translate}
    except json.JSONDecodeError:
        print(f"  WARN: Could not parse Claude translation response")

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

    print(f"Will process {len(to_process)} species")

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
    for i, (sci_name, english_name, ebird_desc, wiki_extract) in enumerate(to_process):
        print(f"  [{i+1}/{len(to_process)}] {sci_name} ({english_name})...", flush=True)

        # Generate English description
        desc_en = generate_description(
            sci_name, english_name,
            ebird_description=ebird_desc,
            wikipedia_summary=wiki_extract,
        )
        time.sleep(delay)

        if not desc_en:
            print(f"    SKIP: Claude returned no description")
            existing[sci_name] = {"description_en": None, "error": "no_response"}
            processed += 1
            continue

        record = {"description_en": desc_en}
        print(f"    EN: {len(desc_en)} chars", end="", flush=True)

        # Translate to other locales
        if not args.skip_translate:
            translations = translate_batch(desc_en, locales, species_name=sci_name)
            time.sleep(delay)
            record["translations"] = translations
            print(f", translated to {len(translations)} locales")
        else:
            print()

        existing[sci_name] = record
        processed += 1

        if processed % args.save_every == 0:
            save_data(existing)
            print(f"  --- Saved progress ({processed} processed) ---")

    save_data(existing)
    print(f"\nDone! Processed {processed} species.")
    print(f"Total in {OUTPUT_FILE.name}: {len(existing)} entries")


if __name__ == "__main__":
    main()