"""
Shared configuration loader for the species-data pipeline.

Reads config.yml from the project root and provides typed access
to all settings. All scripts should use this instead of hardcoding values.

Usage:
    from config import load_config
    cfg = load_config()
    print(cfg["claude"]["locales"])  # ['en', 'de', 'fr', ...]
    print(cfg["inat"]["per_page"])   # 200
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yml"


def load_config() -> dict:
    """Load and return the pipeline configuration from config.yml."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_FILE}")

    text = CONFIG_FILE.read_text(encoding="utf-8")
    return yaml.safe_load(text)


def get_locales() -> list[str]:
    """Shorthand: return the list of Claude translation target locales."""
    return load_config()["claude"]["locales"]


def get_taxon_groups() -> list[dict]:
    """Shorthand: return the list of taxon group dicts."""
    return load_config()["taxon_groups"]


# Map locale codes to human-readable language names (used by Claude translations).
LOCALE_NAMES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "pt": "Portuguese", "it": "Italian", "nl": "Dutch", "pl": "Polish",
    "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "cs": "Czech", "ja": "Japanese", "zh": "Chinese (Simplified)",
    "ru": "Russian", "ko": "Korean", "ar": "Arabic", "hi": "Hindi",
    "tr": "Turkish", "uk": "Ukrainian", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "ms": "Malay", "hu": "Hungarian", "ro": "Romanian",
    "el": "Greek", "bg": "Bulgarian", "hr": "Croatian", "sk": "Slovak",
    "sl": "Slovenian", "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian",
    "he": "Hebrew", "fa": "Persian", "bn": "Bengali", "ta": "Tamil",
}
