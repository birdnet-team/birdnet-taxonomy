"""
Shared utilities for the species-data pipeline.

Provides common infrastructure used by all collectors and build steps:
  - Path constants (ROOT, RAW_DIR)
  - RateLimiter (thread-safe token-bucket)
  - Atomic JSON load/save
  - Graceful shutdown handling (SIGINT)
  - HTTP request caching (disk-backed)
"""

import hashlib
import csv
import json
import os
import re
import signal
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw_data"
MANUAL_ALIASES_FILE = ROOT / "overrides" / "species_aliases.csv"

USER_AGENT = "BirdNET Species Metadata Crawler (https://github.com/birdnet-team/species-data)"


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()


def setup_shutdown():
    """Install SIGINT handler for graceful shutdown.

    First Ctrl-C sets the event; second forces exit.
    Returns the Event so callers can check `is_set()`.
    """
    def _handler(sig, frame):
        if _shutdown_event.is_set():
            raise SystemExit(1)
        _shutdown_event.set()
        print("\n⏎ Interrupt received — finishing current work and saving...")

    signal.signal(signal.SIGINT, _handler)
    return _shutdown_event


def is_shutting_down() -> bool:
    """Check whether a graceful shutdown has been requested."""
    return _shutdown_event.is_set()


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket rate limiter (thread-safe)."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


# ---------------------------------------------------------------------------
# Atomic JSON I/O
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    """Load a JSON file, returning {} if it doesn't exist."""
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(data, path: Path):
    """Atomically write data to a JSON file.

    Writes to a .tmp file, flushes, fsyncs, then renames — so the file
    is never in a half-written state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_FULL_SPECIES_NAME_RE = re.compile(r"^[A-Z][A-Za-z.-]+ [a-z][A-Za-z.-]+$")
_BAD_DISPLAY_NAME_CHARS_RE = re.compile(r"[\(\)\[\]\{\}/\\<>|]")


def is_full_species_name(name: str) -> bool:
    """Return True for canonical binomial species names only."""
    return bool(_FULL_SPECIES_NAME_RE.match((name or "").strip()))


def is_clean_scientific_name(name: str) -> bool:
    """Return True for final scientific names allowed in metadata."""
    clean = (name or "").strip()
    if not clean or _BAD_DISPLAY_NAME_CHARS_RE.search(clean):
        return False
    return is_full_species_name(clean)


def is_clean_common_name(name: str) -> bool:
    """Return True for final common names allowed in metadata."""
    clean = (name or "").strip()
    if not clean or _BAD_DISPLAY_NAME_CHARS_RE.search(clean):
        return False
    return not re.search(r"\s{2,}", clean)


def clean_aliases(names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Filter and dedupe alternate scientific names for final metadata."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = (raw or "").strip()
        if not is_clean_scientific_name(name) or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def load_avilist_species(csv_path: Path) -> dict[str, dict]:
    """Load species-rank AviList rows keyed by scientific name."""
    species: dict[str, dict] = {}
    if not csv_path.exists():
        return species
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("Taxon_rank") != "species":
                continue
            sci = (row.get("Scientific_name") or "").strip()
            if not is_clean_scientific_name(sci):
                continue
            species[sci] = {
                "taxon_group": "Aves",
                "inat_id": None,
                "preferred_common_name": (
                    row.get("English_name_AviList")
                    or row.get("English_name_Clements_v2024")
                    or ""
                ).strip(),
                "ebird_code": (row.get("Species_code_Cornell_Lab") or "").strip(),
            }
    return species


def load_canonical_species(cfg: dict | None = None,
                           group: str = "") -> dict[str, dict]:
    """Load the broadest available canonical species worklist.

    Prefers raw_data/taxonomy.json because it reflects build inclusion rules.
    Falls back to raw_data/inat_data.json plus AviList species when taxonomy is
    unavailable.
    """
    taxonomy_path = RAW_DIR / "taxonomy.json"
    species: dict[str, dict] = {}
    manual_aliases = load_manual_species_aliases()
    if taxonomy_path.exists():
        taxonomy = load_json(taxonomy_path)
        for sci, rec in taxonomy.items():
            if not is_clean_scientific_name(sci):
                continue
            if group and rec.get("taxon_group") != group:
                continue
            species[sci] = _with_manual_aliases(sci, rec, manual_aliases)
        return species

    inat_path = RAW_DIR / "inat_data.json"
    if inat_path.exists():
        inat = load_json(inat_path)
        for sci, rec in inat.items():
            if not is_clean_scientific_name(sci):
                continue
            if group and rec.get("taxon_group") != group:
                continue
            species[sci] = _with_manual_aliases(sci, rec, manual_aliases)

    csv_name = (cfg or {}).get("avilist", {}).get("csv_file", "")
    if csv_name and (not group or group == "Aves"):
        for sci, rec in load_avilist_species(RAW_DIR / csv_name).items():
            if not group or rec.get("taxon_group") == group:
                species.setdefault(sci, _with_manual_aliases(sci, rec, manual_aliases))

    return species


def load_manual_species_aliases() -> dict[str, list[str]]:
    """Load reviewed species aliases for collector fallback lookups."""
    if not MANUAL_ALIASES_FILE.exists():
        return {}
    aliases: dict[str, list[str]] = {}
    with MANUAL_ALIASES_FILE.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sci = (row.get("scientific_name") or "").strip()
            alias = (row.get("alias") or "").strip()
            if not is_clean_scientific_name(sci) or not is_clean_scientific_name(alias):
                continue
            aliases.setdefault(sci, []).append(alias)
    return {sci: clean_aliases(values) for sci, values in aliases.items()}


def _with_manual_aliases(
    sci: str,
    rec: dict,
    manual_aliases: dict[str, list[str]],
) -> dict:
    aliases = clean_aliases([
        *(rec.get("scientific_name_aliases", []) or []),
        *manual_aliases.get(sci, []),
    ])
    if not aliases:
        return rec
    merged = dict(rec)
    merged["scientific_name_aliases"] = aliases
    return merged


# ---------------------------------------------------------------------------
# HTTP request cache (disk-backed)
# ---------------------------------------------------------------------------

_REQUEST_CACHE_DIR = RAW_DIR / ".request_cache"


def cache_key(prefix: str, data: str) -> Path:
    """Build a cache file path from a prefix and hashable data string."""
    h = hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
    return _REQUEST_CACHE_DIR / f"{prefix}_{h}.json"


def cache_get(key: Path):
    """Return cached JSON value or None if not cached."""
    if key.exists():
        with open(key, encoding="utf-8") as f:
            return json.load(f)
    return None


def cache_put(key: Path, value):
    """Write a JSON-serialisable value to the cache."""
    _REQUEST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = key.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    os.replace(tmp, key)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Normalize locale codes across sources to canonical forms.
LOCALE_NORMALIZE: dict[str, str] = {
    "nb": "no",
    "pt-br": "pt",
}

# Acceptable photo licenses (CC variants, public domain, GFDL).
ACCEPTABLE_LICENSES = frozenset({
    "cc0", "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa",
    "cc-by-nd", "cc-by-nc-nd", "pd", "gfdl",
})


def strip_html_tags(text: str) -> str:
    """Remove HTML tags from text."""
    return re.sub(r"<[^>]+>", "", text).strip()
