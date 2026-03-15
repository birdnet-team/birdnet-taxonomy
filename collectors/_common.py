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


def is_full_species_name(name: str) -> bool:
    """Return True for canonical binomial species names only."""
    return bool(_FULL_SPECIES_NAME_RE.match((name or "").strip()))


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
