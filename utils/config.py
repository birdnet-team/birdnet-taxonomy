"""
Shared configuration loader for the species-data pipeline.

Reads config.yml from the project root and provides typed access
to all settings. All scripts should use this instead of hardcoding values.

Usage:
    from utils.config import load_config
    cfg = load_config()
    print(cfg["locales"])       # ['en', 'de', 'fr', ...]
    print(cfg["inat"]["per_page"])  # 200
"""

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yml"


def _parse_yaml_simple(text: str) -> dict:
    """Minimal YAML-subset parser for when PyYAML is not installed.

    Handles the flat structure of our config.yml:
    - top-level keys with scalar values
    - top-level keys with list values (- item)
    - one level of nested dicts
    - quoted strings
    """
    result: dict = {}
    current_key = None
    current_dict: dict | None = None
    in_list = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(stripped)

        # Top-level key (no indent)
        if indent == 0 and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            in_list = False
            current_dict = None
            if value:
                result[key] = _parse_scalar(value)
                current_key = None
            else:
                current_key = key
                result[key] = None  # will be replaced by list or dict
            continue

        # Indented content belongs to current_key
        if current_key is not None:
            # List item
            if stripped.startswith("- "):
                if result[current_key] is None:
                    result[current_key] = []
                    in_list = True
                    current_dict = None
                if in_list and not isinstance(result[current_key], list):
                    pass  # nested list inside dict entry
                if isinstance(result[current_key], list) and current_dict is None:
                    item_text = stripped[2:].strip()
                    result[current_key].append(_parse_scalar(item_text))
                elif isinstance(result[current_key], list) and current_dict is not None:
                    # list item inside a dict-list (like taxon_groups)
                    pass
                continue

            # Dict-style list item (  - name: value)
            if stripped.startswith("- ") and ":" in stripped:
                if result[current_key] is None:
                    result[current_key] = []
                entry_text = stripped[2:].strip()
                k, _, v = entry_text.partition(":")
                current_dict = {k.strip(): _parse_scalar(v.strip())}
                result[current_key].append(current_dict)
                in_list = False
                continue

            # Nested key: value
            if ":" in stripped and not stripped.startswith("-"):
                k, _, v = stripped.partition(":")
                k = k.strip()
                v = v.strip()
                # Is this a continuation of a dict-list item?
                if isinstance(result[current_key], list) and current_dict is not None:
                    current_dict[k] = _parse_scalar(v)
                else:
                    # Regular nested dict
                    if result[current_key] is None:
                        result[current_key] = {}
                        in_list = False
                    if isinstance(result[current_key], dict):
                        result[current_key][k] = _parse_scalar(v)
                continue

    return result


def _parse_scalar(value: str):
    """Parse a scalar YAML value."""
    if not value:
        return ""
    # Remove inline comments
    if "  #" in value:
        value = value[:value.index("  #")].strip()
    # Quoted string
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    # Boolean
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false",):
        return False
    # "no" is special — could be Norwegian locale code, only treat as bool
    # if it's clearly a boolean context (we handle this by quoting in YAML)
    if value == "no":
        return False
    # Integer
    try:
        return int(value)
    except ValueError:
        pass
    # Float
    try:
        return float(value)
    except ValueError:
        pass
    return value


def load_config() -> dict:
    """Load and return the pipeline configuration from config.yml."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_FILE}")

    text = CONFIG_FILE.read_text(encoding="utf-8")

    if yaml is not None:
        cfg = yaml.safe_load(text)
    else:
        cfg = _parse_yaml_simple(text)

    return cfg


def get_locales() -> list[str]:
    """Shorthand: return the list of target locale codes."""
    return load_config()["locales"]


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
