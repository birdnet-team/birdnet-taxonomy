"""
Shared configuration loader for the species-data pipeline.

Reads config.yml from the project root and provides typed access
to all settings. All scripts should use this instead of hardcoding values.

Usage:
    from config import load_config
    cfg = load_config()
    print(cfg["llm"]["locales"])  # ['en', 'de', 'fr', ...]
    print(cfg["inat"]["per_page"])   # 200
"""

import os
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
    """Shorthand: return the list of LLM translation target locales."""
    cfg = load_config()
    llm_cfg = cfg.get("llm") or cfg.get("claude", {})
    return llm_cfg.get("locales", ["en"])


def get_taxon_groups() -> list[dict]:
    """Shorthand: return the list of taxon group dicts."""
    return load_config()["taxon_groups"]


def get_taxonomy_version() -> str:
    """Shorthand: return the taxonomy version string."""
    return str(load_config().get("taxonomy", {}).get("version", "")).strip()


# Map locale codes to human-readable language names.
# Used by Claude translations, the web UI, and anywhere a display name is needed.
LOCALE_NAMES: dict[str, str] = {
    "af": "Afrikaans", "ar": "Arabic", "bg": "Bulgarian", "bn": "Bengali",
    "ca": "Catalan", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish",
    "es_AR": "Spanish (Argentina)", "es_CL": "Spanish (Chile)",
    "es_CR": "Spanish (Costa Rica)", "es_CU": "Spanish (Cuba)",
    "es_DO": "Spanish (Dominican Republic)", "es_EC": "Spanish (Ecuador)",
    "es_ES": "Spanish (Spain)", "es_MX": "Spanish (Mexico)",
    "es_PA": "Spanish (Panama)", "es_PR": "Spanish (Puerto Rico)",
    "et": "Estonian", "eu": "Basque", "fa": "Persian", "fi": "Finnish",
    "fr": "French", "gl": "Galician", "gu": "Gujarati",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian",
    "hy": "Armenian", "id": "Indonesian", "is": "Icelandic", "it": "Italian",
    "ja": "Japanese", "ka": "Georgian", "kk": "Kazakh", "kn": "Kannada",
    "ko": "Korean", "lt": "Lithuanian", "lv": "Latvian",
    "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay",
    "nl": "Dutch", "no": "Norwegian", "pl": "Polish",
    "pt": "Portuguese", "pt_PT": "Portuguese (Portugal)",
    "ro": "Romanian", "ru": "Russian",
    "sk": "Slovak", "sl": "Slovenian", "sq": "Albanian", "sr": "Serbian",
    "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "te": "Telugu",
    "th": "Thai", "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese",
    "zh": "Chinese (Simplified)", "zh_TRA": "Chinese (Traditional)",
    "zu": "Zulu",
}


# ---------------------------------------------------------------------------
# Deployment / environment helpers
# ---------------------------------------------------------------------------

def load_env_value(name: str) -> str:
    """Load a single env value from .env first, then process env vars."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return os.environ.get(name, "").strip().strip("\"'")


def load_root_path() -> str:
    """Load and normalize the deployment URL prefix."""
    root_path = load_env_value("ROOT_PATH")
    if not root_path or root_path == "/":
        return ""
    return "/" + root_path.strip("/")


def load_host_name() -> str:
    """Load and normalize the public host name used for absolute URLs."""
    return load_env_value("HOST_NAME").rstrip("/")


def image_url_prefix() -> str:
    """Absolute image URL prefix, or a relative root-path prefix if no host is set."""
    host = load_host_name()
    root_path = load_root_path()
    if host:
        return f"{host}{root_path}"
    return root_path
