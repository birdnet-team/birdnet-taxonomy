#!/usr/bin/env python3
"""
FastAPI web server for browsing and querying species metadata.

Provides:
  - HTML pages: home/search, species detail
  - REST API: /api/species, /api/species/{name}, /api/search, /api/stats
  - Image proxy: /api/image/{scientific_name}/{size}  (WebP, cached)
  - Auto-generated docs at /docs (Swagger) and /redoc

Usage:
    python server.py                    # start on port 8000
    python server.py --port 3000        # custom port
    python server.py --dev              # load from dev/ instead of dist/
    uvicorn server:app --reload         # development with auto-reload
"""

import argparse
import hashlib
import io
import json
import re
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query, Request as FRequest
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from utils.config import load_config

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"

USER_AGENT = "BirdNET-SpeciesData/1.0"

# ---------------------------------------------------------------------------
# Known locale display names (for UI).  Dynamically extended at load time
# from whatever locales appear in the data.
# ---------------------------------------------------------------------------
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
    "sv": "Swedish", "ta": "Tamil", "te": "Telugu", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese",
    "zh": "Chinese (Simplified)", "zh_TRA": "Chinese (Traditional)",
    "zu": "Zulu",
}

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

_species_list: list[dict] = []
_species_by_name: dict[str, dict] = {}
_species_by_common: dict[str, dict] = {}
_search_index: list[tuple[str, str, dict]] = []  # (lower_sci, lower_common, record)
_all_locales: list[tuple[str, str]] = []  # (code, display_name) sorted


def load_data(dev: bool = False):
    """Load species_metadata.json into memory."""
    global _species_list, _species_by_name, _species_by_common
    global _search_index, _all_locales

    for d in (["dev", "dist"] if dev else ["dist", "dev"]):
        path = ROOT / d / "species_metadata.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                _species_list = json.load(f)
            print(f"Loaded {len(_species_list)} species from {path}")
            break
    else:
        print("WARNING: No species_metadata.json found. Run merge.py first.")
        _species_list = []

    _species_by_name = {}
    _species_by_common = {}
    _search_index = []

    # Discover all locales from the data
    locale_set: set[str] = set()

    for rec in _species_list:
        sci = rec.get("scientific_name", "")
        common = rec.get("common_name", "")
        if sci:
            _species_by_name[sci] = rec
            _species_by_name[sci.lower()] = rec
        if common:
            _species_by_common[common.lower()] = rec
        # Build search index: normalised text for fuzzy matching
        search_text = _normalise(f"{sci} {common}")
        for name in rec.get("common_names", {}).values():
            search_text += " " + _normalise(name)
        _search_index.append((sci.lower(), search_text, rec))
        locale_set.update(rec.get("common_names", {}).keys())

    # Build sorted locale list for UI (exclude 'en' since it's default)
    locale_set.discard("en")
    _all_locales = sorted(
        [(code, LOCALE_NAMES.get(code, code)) for code in locale_set],
        key=lambda x: x[1],
    )
    print(f"  {len(_all_locales)} locales discovered from data")


def _normalise(text: str) -> str:
    """Normalise text for search: lowercase, strip accents."""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


# ---------------------------------------------------------------------------
# Image proxy helpers
# ---------------------------------------------------------------------------

_image_cache_dir: Path = ROOT / ".image_cache"
_image_sizes: dict[str, tuple[int, int]] = {}
_image_quality: int = 80


def _init_image_config():
    """Load image proxy settings from config."""
    global _image_cache_dir, _image_sizes, _image_quality
    cfg = load_config()
    img = cfg.get("images", {})
    _image_cache_dir = ROOT / img.get("cache_dir", ".image_cache")
    _image_cache_dir.mkdir(parents=True, exist_ok=True)
    _image_quality = img.get("quality", 80)
    _image_sizes = {
        "thumb": (img.get("thumb_width", 150), img.get("thumb_height", 100)),
        "medium": (img.get("medium_width", 480), img.get("medium_height", 320)),
        "large": (img.get("large_width", 1200), img.get("large_height", 800)),
    }


def _cache_key(url: str, size: str) -> str:
    """Deterministic cache filename from URL + size."""
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return f"{h}_{size}.webp"


def _center_crop_3_2(img):
    """Center-crop an image to 3:2 aspect ratio."""
    w, h = img.size
    target_ratio = 3 / 2
    current_ratio = w / h
    if abs(current_ratio - target_ratio) < 0.01:
        return img
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))


def _fetch_and_convert(url: str, size: str) -> bytes | None:
    """Fetch image from URL, crop, resize, convert to WebP."""
    from PIL import Image

    target = _image_sizes.get(size)
    if not target:
        return None

    # Download
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            raw_bytes = resp.read()
    except (HTTPError, URLError, TimeoutError):
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
    except Exception:
        return None

    # Center crop to 3:2, then resize
    img = _center_crop_3_2(img)
    img.thumbnail(target, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=_image_quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    if not _species_list:
        load_data()
    _init_image_config()
    yield


app = FastAPI(
    title="BirdNET Species Metadata API",
    description="Browse and query species metadata for BirdNET models.",
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Serve static assets (logo, etc.)
STATIC_DIR = ROOT / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Image proxy endpoint
# ---------------------------------------------------------------------------

@app.get("/api/image/{scientific_name:path}/{size}",
         tags=["Images"],
         responses={200: {"content": {"image/webp": {}}}})
async def image_proxy(scientific_name: str, size: str):
    """Serve a species image as WebP in the requested size.

    Sizes: thumb (150×100), medium (480×320), large (1200×800).
    Images are fetched from the original source, converted to WebP,
    center-cropped to 3:2, and cached on disk.
    """
    if size not in _image_sizes:
        raise HTTPException(400, f"Invalid size '{size}'. Use: thumb, medium, large")

    rec = _species_by_name.get(scientific_name) or \
          _species_by_name.get(scientific_name.lower())
    if not rec:
        raise HTTPException(404, "Species not found")

    source_url = rec.get("image_url", "")
    if not source_url:
        raise HTTPException(404, "No image available for this species")

    # Check disk cache
    cache_file = _image_cache_dir / _cache_key(source_url, size)
    if cache_file.exists():
        return Response(
            content=cache_file.read_bytes(),
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Fetch, convert, cache
    webp_bytes = _fetch_and_convert(source_url, size)
    if not webp_bytes:
        raise HTTPException(502, "Failed to fetch or convert source image")

    # Write to cache (atomic)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_bytes(webp_bytes)
    tmp.replace(cache_file)

    return Response(
        content=webp_bytes,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: FRequest, q: str = "", group: str = "",
               lang: str = "", sort: str = "", page: int = 1, per_page: int = 50):
    """Home page with search and species listing."""
    results = _search(q, group)

    # Sort results
    if sort == "a-z":
        results = sorted(results, key=lambda r: (r.get("common_name") or r.get("scientific_name", "")).lower())
    elif sort == "z-a":
        results = sorted(results, key=lambda r: (r.get("common_name") or r.get("scientific_name", "")).lower(), reverse=True)
    elif sort == "obs":
        results = sorted(results, key=lambda r: r.get("observations_count", 0) or 0, reverse=True)

    total = len(results)

    # Pagination
    start = (page - 1) * per_page
    end = start + per_page
    page_results = results[start:end]
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Available groups
    groups = sorted(set(r.get("taxon_group", "") for r in _species_list if r.get("taxon_group")))

    return templates.TemplateResponse("home.html", {
        "request": request,
        "species": page_results,
        "query": q,
        "group": group,
        "lang": lang,
        "sort": sort,
        "locales": _all_locales,
        "groups": groups,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


@app.get("/species/{scientific_name:path}", response_class=HTMLResponse,
         include_in_schema=False)
async def species_page(request: FRequest, scientific_name: str):
    """Species detail page."""
    rec = _species_by_name.get(scientific_name) or _species_by_name.get(scientific_name.lower())
    if not rec:
        raise HTTPException(status_code=404, detail="Species not found")

    return templates.TemplateResponse("species.html", {
        "request": request,
        "s": rec,
    })


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/stats", tags=["API"])
async def api_stats():
    """Pipeline statistics: total species, counts per taxon group."""
    from collections import Counter
    groups = Counter(r.get("taxon_group", "unknown") for r in _species_list)
    return {
        "total_species": len(_species_list),
        "groups": dict(sorted(groups.items())),
        "locales": [code for code, _ in _all_locales],
    }


@app.get("/api/groups", tags=["API"])
async def api_groups():
    """List available taxon groups with species counts."""
    from collections import Counter
    groups = Counter(r.get("taxon_group", "unknown") for r in _species_list)
    return [{"name": g, "count": c} for g, c in sorted(groups.items())]


@app.get("/api/search", tags=["API"])
async def api_search(
    q: str = Query("", description="Search query (scientific or common name)"),
    group: str = Query("", description="Filter by taxon group"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=500, description="Results per page"),
):
    """Search species by name. Returns paginated results."""
    results = _search(q, group)
    total = len(results)
    start = (page - 1) * per_page
    return {
        "query": q,
        "group": group,
        "total": total,
        "page": page,
        "per_page": per_page,
        "results": results[start:start + per_page],
    }


@app.get("/api/species", tags=["API"])
async def api_species_list(
    group: str = Query("", description="Filter by taxon group"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=500, description="Results per page"),
):
    """List all species, optionally filtered by taxon group."""
    data = _species_list
    if group:
        data = [r for r in data if r.get("taxon_group", "").lower() == group.lower()]
    total = len(data)
    start = (page - 1) * per_page
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "results": data[start:start + per_page],
    }


@app.get("/api/species/{scientific_name:path}", tags=["API"])
async def api_species_detail(scientific_name: str):
    """Get full metadata for one species by scientific name."""
    rec = _species_by_name.get(scientific_name) or _species_by_name.get(scientific_name.lower())
    if not rec:
        raise HTTPException(status_code=404, detail="Species not found")
    return rec


# ---------------------------------------------------------------------------
# Search helper
# ---------------------------------------------------------------------------

def _search(q: str, group: str = "") -> list[dict]:
    """Search species by query string and optional group filter."""
    results = _species_list

    if group:
        results = [r for r in results if r.get("taxon_group", "").lower() == group.lower()]

    if not q:
        return results

    q_norm = _normalise(q)
    terms = q_norm.split()

    scored = []
    for sci_lower, search_text, rec in _search_index:
        if group and rec.get("taxon_group", "").lower() != group.lower():
            continue
        # All terms must appear somewhere
        if all(t in search_text for t in terms):
            # Score: exact matches rank higher
            score = 0
            if q_norm == sci_lower:
                score = 100
            elif sci_lower.startswith(q_norm):
                score = 80
            elif q_norm in sci_lower:
                score = 60
            else:
                score = 40
            # Boost by observation count
            score += min(rec.get("observations_count", 0) / 1_000_000, 10)
            scored.append((score, rec))

    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Species metadata web server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--dev", action="store_true",
                        help="Load metadata from dev/ instead of dist/")
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload on code changes")
    args = parser.parse_args()

    load_data(dev=args.dev)
    _init_image_config()
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
