#!/usr/bin/env python3
"""
FastAPI web server for browsing and querying species metadata.

Provides:
  - HTML pages: home/search, species detail
  - REST API: /api/species, /api/species/{name}, /api/search, /api/stats
  - Image proxy: /api/image/{scientific_name}/{size}  (WebP, cached)
  - Auto-generated docs at /docs (Swagger) and /redoc

Usage:
    python -m web.server                   # start on port 8000
    python -m web.server --port 3000       # custom port
    python -m web.server --dev             # load from dev/ instead of dist/
    uvicorn web.server:app --reload        # development with auto-reload
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query, Request as FRequest
from pydantic import BaseModel, Field, field_validator
from utils.images import ImageSize, fetch_cached, image_filename, save_species_image
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import load_config


# ---------------------------------------------------------------------------
# Pydantic response models (for OpenAPI schema + Swagger examples)
# ---------------------------------------------------------------------------

_SPECIES_EXAMPLE: dict[str, Any] = {
    "scientific_name": "Anas platyrhynchos",
    "common_name": "Mallard",
    "taxon_group": "Aves",
    "observations_count": 829418,
    "inat_id": 6930,
    "ebird_code": "mallar3",
    "gbif_id": 9761484,
    "ncbi_id": 8839,
    "avibase_id": "Anas-platyrhynchos",
    "birdlife_id": 22680186,
    "image": {
        "thumb": "/api/image/Anas platyrhynchos/thumb",
        "medium": "/api/image/Anas platyrhynchos/medium",
        "large": "/api/image/Anas platyrhynchos/large",
        "source": "inat",
        "author": "anonymous",
        "license": "cc-by-sa",
    },
    "description_source": "wikipedia",
    "descriptions": {
        "en": "This large, familiar duck inhabits diverse aquatic environments...",
        "de": "Diese große, bekannte Ente bewohnt vielfältige Gewässer...",
    },
    "wikipedia_urls": {
        "en": "https://en.wikipedia.org/wiki/Mallard",
        "de": "https://de.wikipedia.org/wiki/Stockente",
    },
    "common_names": {
        "en": "Mallard",
        "de": "Stockente",
        "fr": "Canard colvert",
        "es": "Ánade azulón",
    },
}


class SpeciesRecord(BaseModel):
    """Full species metadata record."""
    scientific_name: str = Field(..., examples=["Anas platyrhynchos"])
    common_name: str = Field("", examples=["Mallard"])
    taxon_group: str = Field("", examples=["Aves"])
    observations_count: Optional[int] = Field(None, examples=[829418])
    inat_id: Optional[int] = Field(None, examples=[6930])
    ebird_code: Optional[str] = Field(None, examples=["mallar3"])
    gbif_id: Optional[int] = Field(None, examples=[9761484])
    ncbi_id: Optional[int] = Field(None, examples=[8839])
    avibase_id: Optional[str] = Field(None, examples=["Anas-platyrhynchos"])
    birdlife_id: Optional[int] = Field(None, examples=[22680186])

    @field_validator("birdlife_id", "gbif_id", "ncbi_id", "inat_id", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        if v == "":
            return None
        return v
    image: Optional[dict[str, str]] = Field(
        None,
        description="Image proxy URLs and attribution",
        examples=[{
            "thumb": "/api/image/Anas platyrhynchos/thumb",
            "medium": "/api/image/Anas platyrhynchos/medium",
            "large": "/api/image/Anas platyrhynchos/large",
            "source": "inat",
            "author": "anonymous",
            "license": "cc-by-sa",
        }],
    )
    description_source: Optional[str] = Field(None, examples=["wikipedia"])
    descriptions: Optional[dict[str, str]] = Field(None, examples=[{"en": "A large, familiar duck...", "de": "Eine große, bekannte Ente..."}])
    wikipedia_urls: Optional[dict[str, str]] = Field(None, examples=[{"en": "https://en.wikipedia.org/wiki/Mallard", "de": "https://de.wikipedia.org/wiki/Stockente"}])
    common_names: Optional[dict[str, str]] = Field(None, examples=[{"en": "Mallard", "de": "Stockente", "fr": "Canard colvert"}])

    model_config = {"extra": "allow"}


class PaginatedSpecies(BaseModel):
    """Paginated list of species records."""
    total: int = Field(..., examples=[13361])
    page: int = Field(..., examples=[1])
    per_page: int = Field(..., examples=[50])
    results: list[dict[str, Any]] = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "total": 13361,
                "page": 1,
                "per_page": 2,
                "results": [_SPECIES_EXAMPLE],
            }],
        },
    }


class SearchResponse(PaginatedSpecies):
    """Search results with query echo."""
    query: str = Field(..., examples=["mallard"])

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "query": "mallard",
                "total": 1,
                "page": 1,
                "per_page": 50,
                "results": [_SPECIES_EXAMPLE],
            }],
        },
    }


class StatsResponse(BaseModel):
    """Dataset statistics."""
    total_species: int = Field(..., examples=[13361])
    groups: dict[str, int] = Field(..., examples=[{"Aves": 11157, "Mammalia": 1087, "Insecta": 566, "Amphibia": 540, "Reptilia": 11}])
    locales: list[str] = Field(..., examples=[["af", "ar", "bg", "de", "es", "fr", "ja", "ko", "zh"]])


class GroupCount(BaseModel):
    """Taxon group with species count."""
    name: str = Field(..., examples=["Aves"])
    count: int = Field(..., examples=[11157])

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

USER_AGENT = "BirdNET Species Data Bot (https://github.com/birdnet-team/species-data)"

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
        print("WARNING: No species_metadata.json found. "
              "Run: python -m build.metadata")
        _species_list = []

    _species_by_name = {}
    _species_by_common = {}
    _search_index = []

    locale_set: set[str] = set()

    for rec in _species_list:
        sci = rec.get("scientific_name", "")
        common = rec.get("common_name", "")
        if sci:
            _species_by_name[sci] = rec
            _species_by_name[sci.lower()] = rec
        if common:
            _species_by_common[common.lower()] = rec
        search_text = _normalise(f"{sci} {common}")
        for name in rec.get("common_names", {}).values():
            search_text += " " + _normalise(name)
        _search_index.append((sci.lower(), search_text, rec))
        locale_set.update(rec.get("common_names", {}).keys())

    locale_set.discard("en")
    _all_locales = sorted(
        [(code, LOCALE_NAMES.get(code, code)) for code in locale_set],
        key=lambda x: x[1],
    )
    _refresh_field_set()
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

_image_dir: Path = ROOT / "dist" / "images"
_image_cache_dir: Path = ROOT / ".image_cache"
_image_sizes: dict[str, ImageSize] = {}
_image_quality: int = 80
_dev_mode: bool = False


def _init_image_config(dev: bool = False):
    """Load image proxy settings from config."""
    global _image_dir, _image_cache_dir, _image_sizes, _image_quality, _dev_mode
    _dev_mode = dev
    cfg = load_config()
    img = cfg.get("images", {})
    _image_dir = ROOT / ("dev" if dev else "dist") / "images"
    _image_dir.mkdir(parents=True, exist_ok=True)
    _image_cache_dir = ROOT / img.get("cache_dir", ".image_cache")
    _image_cache_dir.mkdir(parents=True, exist_ok=True)
    _image_quality = img.get("quality", 80)
    _image_sizes = {
        "thumb": ImageSize(img.get("thumb_width", 150), img.get("thumb_height", 100)),
        "medium": ImageSize(img.get("medium_width", 480), img.get("medium_height", 320)),
        "large": ImageSize(img.get("large_width", 1200), img.get("large_height", 800)),
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    if not _species_list:
        load_data()
    _init_image_config(dev=_dev_mode)
    yield


app = FastAPI(
    title="BirdNET Species Metadata API",
    description="Browse and query species metadata for BirdNET models.",
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Image proxy endpoint
# ---------------------------------------------------------------------------

@app.get("/api/image/{scientific_name:path}/{size}",
         tags=["Images"],
         responses={200: {"content": {"image/webp": {}}}})
async def image_proxy(scientific_name: str, size: str):
    """Serve a species image as WebP in the requested size.

    Sizes: thumb (150x100), medium (480x320), large (1200x800).
    Images are fetched from the original source, smart-cropped with YOLO,
    and saved to disk with meaningful filenames.  Subsequent requests are
    served directly from the saved file.
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

    sci = rec.get("scientific_name", "")
    common = rec.get("common_name", "")
    author = rec.get("image_author", "")

    # Check for named file on disk first
    fname = image_filename(sci, common, author, size)
    local_path = _image_dir / fname
    if local_path.exists():
        return Response(
            content=local_path.read_bytes(),
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Not on disk — fetch, crop, save with proper name
    saved = save_species_image(
        url=source_url,
        scientific_name=sci,
        common_name=common,
        author=author,
        size_name=size,
        size=_image_sizes[size],
        image_dir=_image_dir,
        quality=_image_quality,
    )
    if saved and saved.exists():
        return Response(
            content=saved.read_bytes(),
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    raise HTTPException(502, "Failed to fetch or convert source image")


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: FRequest, q: str = "", group: str = "",
               lang: str = "", sort: str = "", page: int = 1, per_page: int = 50):
    """Home page with search and species listing."""
    results = _search(q, group)

    if sort == "a-z":
        results = sorted(results, key=lambda r: (r.get("common_name") or r.get("scientific_name", "")).lower())
    elif sort == "z-a":
        results = sorted(results, key=lambda r: (r.get("common_name") or r.get("scientific_name", "")).lower(), reverse=True)
    elif sort == "obs":
        results = sorted(results, key=lambda r: r.get("observations_count", 0) or 0, reverse=True)

    total = len(results)
    start = (page - 1) * per_page
    end = start + per_page
    page_results = results[start:end]
    total_pages = max(1, (total + per_page - 1) // per_page)

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
    rec = _species_by_name.get(scientific_name) or \
          _species_by_name.get(scientific_name.lower())
    if not rec:
        raise HTTPException(status_code=404, detail="Species not found")

    return templates.TemplateResponse("species.html", {
        "request": request,
        "s": rec,
    })


# ---------------------------------------------------------------------------
# REST API — projection, filtering, sorting helpers
# ---------------------------------------------------------------------------

# All top-level keys that exist in species records
_ALL_FIELDS: set[str] = set()


def _refresh_field_set():
    """Rebuild the set of known top-level fields from loaded data.

    Reports API field names (``image`` instead of the internal
    ``image_url`` / ``image_source`` / ``image_author`` / ``image_license``).
    """
    global _ALL_FIELDS
    fields: set[str] = set()
    for rec in _species_list:
        fields.update(rec.keys())
    # Replace internal image_* keys with the API-facing 'image' key
    for k in ("image_url", "image_source", "image_author", "image_license"):
        fields.discard(k)
    fields.add("image")
    _ALL_FIELDS = fields


def _api_record(record: dict) -> dict:
    """Transform a raw data record for API output.

    Replaces the internal ``image_url`` / ``image_source`` / ``image_author`` /
    ``image_license`` fields with a single ``image`` dict that contains proxy
    URLs pointing to our server plus attribution metadata.
    """
    rec = dict(record)  # shallow copy

    sci = rec.get("scientific_name", "")
    url = rec.pop("image_url", None)
    source = rec.pop("image_source", None)
    author = rec.pop("image_author", None)
    lic = rec.pop("image_license", None)

    if url and sci:
        img: dict[str, str] = {
            "thumb": f"/api/image/{sci}/thumb",
            "medium": f"/api/image/{sci}/medium",
            "large": f"/api/image/{sci}/large",
        }
        if source:
            img["source"] = source
        if author:
            img["author"] = author
        if lic:
            img["license"] = lic
        rec["image"] = img
    else:
        rec["image"] = None

    return rec


def _project(record: dict, fields: str | None, exclude: str | None,
             locale: str | None) -> dict:
    """Apply field selection / exclusion and locale filtering to a record.

    ``fields`` and ``exclude`` are comma-separated top-level key names.
    ``locale`` is a comma-separated list of locale codes.  When given,
    ``common_names`` and ``descriptions`` are trimmed to only those locales.
    """
    rec = _api_record(record)

    # --- locale filter ---
    if locale:
        codes = {c.strip() for c in locale.split(",") if c.strip()}
        if codes:
            if "common_names" in rec and isinstance(rec["common_names"], dict):
                rec["common_names"] = {
                    k: v for k, v in rec["common_names"].items() if k in codes
                }
            if "descriptions" in rec and isinstance(rec["descriptions"], dict):
                rec["descriptions"] = {
                    k: v for k, v in rec["descriptions"].items() if k in codes
                }
            if "wikipedia_urls" in rec and isinstance(rec["wikipedia_urls"], dict):
                rec["wikipedia_urls"] = {
                    k: v for k, v in rec["wikipedia_urls"].items() if k in codes
                }

    # --- field inclusion ---
    if fields:
        keys = {f.strip() for f in fields.split(",") if f.strip()}
        rec = {k: v for k, v in rec.items() if k in keys}

    # --- field exclusion ---
    elif exclude:
        keys = {f.strip() for f in exclude.split(",") if f.strip()}
        for k in keys:
            rec.pop(k, None)

    return rec


def _filter_species(data: list[dict], *,
                    group: str = "",
                    has_image: str = "",
                    has_description: str = "",
                    description_source: str = "",
                    min_observations: int | None = None,
                    max_observations: int | None = None) -> list[dict]:
    """Apply boolean / range filters to a species list."""
    result = data

    if group:
        result = [r for r in result
                  if r.get("taxon_group", "").lower() == group.lower()]

    if has_image:
        want = has_image.lower() in ("true", "1", "yes")
        result = [r for r in result if bool(r.get("image_url")) == want]

    if has_description:
        want = has_description.lower() in ("true", "1", "yes")
        result = [r for r in result
                  if bool((r.get("descriptions") or {}).get("en")) == want]

    if description_source:
        sources = {s.strip().lower() for s in description_source.split(",") if s.strip()}
        result = [r for r in result
                  if (r.get("description_source") or "").lower() in sources]

    if min_observations is not None:
        result = [r for r in result
                  if (r.get("observations_count") or 0) >= min_observations]

    if max_observations is not None:
        result = [r for r in result
                  if (r.get("observations_count") or 0) <= max_observations]

    return result


def _sort_species(data: list[dict], sort: str) -> list[dict]:
    """Sort species list by field name.  Prefix with ``-`` for descending."""
    if not sort:
        return data
    desc = sort.startswith("-")
    key = sort.lstrip("-").strip()
    if not key:
        return data

    def sort_key(r: dict):
        v = r.get(key)
        if v is None:
            return (1, "")  # nones last
        if isinstance(v, (int, float)):
            return (0, v)
        return (0, str(v).lower())

    return sorted(data, key=sort_key, reverse=desc)


def _to_csv(records: list[dict], fields: str | None = None) -> str:
    """Convert list of records to CSV string.

    Nested dicts (common_names, descriptions) are JSON-encoded in cells.
    """
    if not records:
        return ""

    if fields:
        columns = [f.strip() for f in fields.split(",") if f.strip()]
    else:
        # Deterministic column order: fixed columns first, rest sorted
        priority = [
            "scientific_name", "common_name", "taxon_group",
            "observations_count", "inat_id", "ebird_code",
        ]
        all_keys: set[str] = set()
        for r in records:
            all_keys.update(r.keys())
        columns = [c for c in priority if c in all_keys]
        columns += sorted(all_keys - set(columns))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        row = {}
        for c in columns:
            v = rec.get(c)
            if isinstance(v, dict):
                row[c] = json.dumps(v, ensure_ascii=False)
            elif v is None:
                row[c] = ""
            else:
                row[c] = v
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/stats", tags=["API"], response_model=StatsResponse)
async def api_stats():
    """Pipeline statistics: total species, counts per taxon group."""
    from collections import Counter
    groups = Counter(r.get("taxon_group", "unknown") for r in _species_list)
    return {
        "total_species": len(_species_list),
        "groups": dict(sorted(groups.items())),
        "locales": [code for code, _ in _all_locales],
    }


@app.get("/api/groups", tags=["API"], response_model=list[GroupCount])
async def api_groups():
    """List available taxon groups with species counts."""
    from collections import Counter
    groups = Counter(r.get("taxon_group", "unknown") for r in _species_list)
    return [{"name": g, "count": c} for g, c in sorted(groups.items())]


@app.get("/api/fields", tags=["API"], response_model=list[str])
async def api_fields():
    """List all available top-level field names in species records.

    Returns the names of every top-level key that appears in at least one
    species record.  Use these names with the `fields` and `exclude`
    query parameters on other endpoints.
    """
    return sorted(_ALL_FIELDS)


@app.get("/api/search", tags=["API"],
         response_model=SearchResponse,
         response_model_exclude_none=True)
async def api_search(
    q: str = Query("", description="Search query (scientific or common name)"),
    group: str = Query("", description="Filter by taxon group"),
    has_image: str = Query("", description="Filter: true/false"),
    has_description: str = Query("", description="Filter: true/false"),
    description_source: str = Query("", description="Filter by source (wikipedia, ebird)"),
    min_observations: Optional[int] = Query(None, description="Minimum observation count"),
    max_observations: Optional[int] = Query(None, description="Maximum observation count"),
    sort: str = Query("", description="Sort field (prefix '-' for desc, e.g. -observations_count)"),
    fields: Optional[str] = Query(None, description="Comma-separated fields to include"),
    exclude: Optional[str] = Query(None, description="Comma-separated fields to exclude"),
    locale: Optional[str] = Query(None, description="Comma-separated locale codes for common_names/descriptions"),
    format: str = Query("json", description="Response format: json or csv"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=500, description="Results per page"),
):
    """Search species by name with full filtering, sorting, and field selection."""
    results = _search(q, group)
    results = _filter_species(
        results, group=group,
        has_image=has_image, has_description=has_description,
        description_source=description_source,
        min_observations=min_observations, max_observations=max_observations,
    )
    results = _sort_species(results, sort)

    total = len(results)
    start = (page - 1) * per_page
    page_results = results[start:start + per_page]
    page_results = [_project(r, fields, exclude, locale) for r in page_results]

    if format == "csv":
        return Response(
            content=_to_csv(page_results, fields),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=search_results.csv"},
        )

    return {
        "query": q,
        "total": total,
        "page": page,
        "per_page": per_page,
        "results": page_results,
    }


@app.get("/api/species", tags=["API"],
         response_model=PaginatedSpecies,
         response_model_exclude_none=True)
async def api_species_list(
    group: str = Query("", description="Filter by taxon group"),
    has_image: str = Query("", description="Filter: true/false"),
    has_description: str = Query("", description="Filter: true/false"),
    description_source: str = Query("", description="Filter by source (wikipedia, ebird)"),
    min_observations: Optional[int] = Query(None, description="Minimum observation count"),
    max_observations: Optional[int] = Query(None, description="Maximum observation count"),
    sort: str = Query("", description="Sort field (prefix '-' for desc, e.g. -observations_count)"),
    fields: Optional[str] = Query(None, description="Comma-separated fields to include"),
    exclude: Optional[str] = Query(None, description="Comma-separated fields to exclude"),
    locale: Optional[str] = Query(None, description="Comma-separated locale codes for common_names/descriptions"),
    format: str = Query("json", description="Response format: json or csv"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=500, description="Results per page"),
):
    """List all species with filtering, sorting, field selection, and pagination."""
    data = _filter_species(
        _species_list, group=group,
        has_image=has_image, has_description=has_description,
        description_source=description_source,
        min_observations=min_observations, max_observations=max_observations,
    )
    data = _sort_species(data, sort)

    total = len(data)
    start = (page - 1) * per_page
    page_results = data[start:start + per_page]
    page_results = [_project(r, fields, exclude, locale) for r in page_results]

    if format == "csv":
        return Response(
            content=_to_csv(page_results, fields),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=species.csv"},
        )

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "results": page_results,
    }


@app.get("/api/species/{scientific_name:path}", tags=["API"],
         response_model=SpeciesRecord,
         response_model_exclude_none=True)
async def api_species_detail(
    scientific_name: str,
    fields: Optional[str] = Query(None, description="Comma-separated fields to include"),
    exclude: Optional[str] = Query(None, description="Comma-separated fields to exclude"),
    locale: Optional[str] = Query(None, description="Comma-separated locale codes for common_names/descriptions"),
):
    """Get full metadata for one species by scientific name."""
    rec = _species_by_name.get(scientific_name) or \
          _species_by_name.get(scientific_name.lower())
    if not rec:
        raise HTTPException(status_code=404, detail="Species not found")
    return _project(rec, fields, exclude, locale)


# ---------------------------------------------------------------------------
# Search helper
# ---------------------------------------------------------------------------

def _search(q: str, group: str = "") -> list[dict]:
    """Search species by query string and optional group filter."""
    results = _species_list

    if group:
        results = [r for r in results
                   if r.get("taxon_group", "").lower() == group.lower()]

    if not q:
        return results

    q_norm = _normalise(q)
    terms = q_norm.split()

    scored = []
    for sci_lower, search_text, rec in _search_index:
        if group and rec.get("taxon_group", "").lower() != group.lower():
            continue
        if all(t in search_text for t in terms):
            score = 0
            if q_norm == sci_lower:
                score = 100
            elif sci_lower.startswith(q_norm):
                score = 80
            elif q_norm in sci_lower:
                score = 60
            else:
                score = 40
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
    _init_image_config(dev=args.dev)
    uvicorn.run(
        "web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
