#!/usr/bin/env python3
"""
FastAPI web server for browsing and querying species metadata.

Provides:
  - HTML pages: home/search, species detail
  - REST API: /api/species, /api/species/{name}, /api/search, /api/stats
  - Image proxy: /api/image/{scientific_name}?size=  (WebP, cached)
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
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request as FRequest
from pydantic import BaseModel, Field, field_validator
from utils.images import ImageSize, save_species_image
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from config import (
    ROOT, LOCALE_NAMES, get_taxonomy_version, load_config,
    load_root_path, load_host_name,
)


# ---------------------------------------------------------------------------
# Pydantic response models (for OpenAPI schema + Swagger examples)
# ---------------------------------------------------------------------------

_SPECIES_EXAMPLE: dict[str, Any] = {
    "taxonomy_version": "v2025-11Jun",
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
        "src": "https://static.inaturalist.org/photos/123/original.jpeg",
        "thumb": "https://birdnet.cornell.edu/taxonomy/api/image/Anas%20platyrhynchos?size=thumb",
        "medium": "https://birdnet.cornell.edu/taxonomy/api/image/Anas%20platyrhynchos?size=medium",
        "source": "inat",
        "author": "anonymous",
        "license": "cc-by-sa",
    },
    "description_source": "wikipedia",
    "claude_locales": ["pt"],
    "descriptions": {
        "en": "This large, familiar duck inhabits diverse aquatic environments...",
        "de": "Diese große, bekannte Ente bewohnt vielfältige Gewässer...",
        "pt": "Este pato grande e familiar habita diversos ambientes aquáticos...",
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

_SPECIES_LIST_EXAMPLE: dict[str, Any] = {
    k: v for k, v in _SPECIES_EXAMPLE.items() if k != "taxonomy_version"
}


class SpeciesRecord(BaseModel):
    """Full species metadata record."""
    taxonomy_version: Optional[str] = Field(None, examples=["v2025-11Jun"])
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
            "src": "https://static.inaturalist.org/photos/123/original.jpeg",
            "thumb": "https://birdnet.cornell.edu/taxonomy/api/image/Anas%20platyrhynchos?size=thumb",
            "medium": "https://birdnet.cornell.edu/taxonomy/api/image/Anas%20platyrhynchos?size=medium",
            "source": "inat",
            "author": "anonymous",
            "license": "cc-by-sa",
        }],
    )
    description_source: Optional[str] = Field(None, examples=["wikipedia"])
    image_crop_anchor: Optional[int] = Field(
        None,
        description="Optional manual 3x3 crop anchor for the species image (1=top-left, 5=center, 9=bottom-right)",
        examples=[5],
    )
    claude_locales: Optional[list[str]] = Field(
        None,
        description="Locales whose description text is provided by Claude rather than the base source",
        examples=[["pt"]],
    )
    descriptions: Optional[dict[str, str]] = Field(None, examples=[{"en": "A large, familiar duck...", "de": "Eine große, bekannte Ente..."}])
    wikipedia_urls: Optional[dict[str, str]] = Field(None, examples=[{"en": "https://en.wikipedia.org/wiki/Mallard", "de": "https://de.wikipedia.org/wiki/Stockente"}])
    common_names: Optional[dict[str, str]] = Field(None, examples=[{"en": "Mallard", "de": "Stockente", "fr": "Canard colvert"}])

    model_config = {"extra": "allow"}


class PaginatedSpecies(BaseModel):
    """Paginated list of species records."""
    taxonomy_version: str = Field(..., examples=["v2025-11Jun"])
    total: int = Field(..., examples=[13361])
    page: int = Field(..., examples=[1])
    per_page: int = Field(..., examples=[50])
    results: list[dict[str, Any]] = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "taxonomy_version": "v2025-11Jun",
                "total": 13361,
                "page": 1,
                "per_page": 2,
                "results": [_SPECIES_LIST_EXAMPLE],
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
                "taxonomy_version": "v2025-11Jun",
                "total": 1,
                "page": 1,
                "per_page": 50,
                "results": [_SPECIES_LIST_EXAMPLE],
            }],
        },
    }


class StatsResponse(BaseModel):
    """Dataset statistics."""
    taxonomy_version: str = Field(..., examples=["v2025-11Jun"])
    total_species: int = Field(..., examples=[13361])
    groups: dict[str, int] = Field(..., examples=[{"Aves": 11157, "Mammalia": 1087, "Insecta": 566, "Amphibia": 540, "Reptilia": 11}])
    locales: list[str] = Field(..., examples=[["af", "ar", "bg", "de", "es", "fr", "ja", "ko", "zh"]])


class GroupCount(BaseModel):
    """Taxon group with species count."""
    name: str = Field(..., examples=["Aves"])
    count: int = Field(..., examples=[11157])

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _with_root_path(path: str) -> str:
    """Prefix a local path with the configured deployment root."""
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{_root_path}{path}" if _root_path else path


def _with_image_prefix(path: str) -> str:
    """Prefix an image/API path with HOST_NAME and ROOT_PATH when available."""
    rooted = _with_root_path(path)
    return f"{_host_name}{rooted}" if _host_name else rooted


def _quote_path(value: Any) -> str:
    """Quote a path segment for safe inclusion in URLs."""
    return quote(str(value), safe="")


_root_path = load_root_path()
_host_name = load_host_name()
_taxonomy_version = get_taxonomy_version()
_site_title = f"BirdNET+ Taxonomy {_taxonomy_version}".strip()

USER_AGENT = "BirdNET Species Data Bot (https://github.com/birdnet-team/species-data)"

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

_species_list: list[dict] = []
_species_by_name: dict[str, dict] = {}
_species_by_common: dict[str, dict] = {}
_species_by_ebird: dict[str, dict] = {}
_species_by_inat_id: dict[int, dict] = {}
_search_index: list[tuple[str, str, dict]] = []  # (lower_sci, lower_common, record)
_all_locales: list[tuple[str, str]] = []  # (code, display_name) sorted


def _find_species(identifier: str) -> dict | None:
    """Resolve a species by scientific name, common name, eBird code, or iNat ID."""
    # Scientific name (exact, case-insensitive)
    rec = _species_by_name.get(identifier) or _species_by_name.get(identifier.lower())
    if rec:
        return rec
    # Common name (case-insensitive)
    rec = _species_by_common.get(identifier.lower())
    if rec:
        return rec
    # eBird code (case-insensitive)
    rec = _species_by_ebird.get(identifier.lower())
    if rec:
        return rec
    # iNat taxon ID (numeric string)
    try:
        rec = _species_by_inat_id.get(int(identifier))
        if rec:
            return rec
    except (ValueError, TypeError):
        pass
    return None


def load_data(dev: bool = False):
    """Load species_metadata.json into memory."""
    global _species_list, _species_by_name, _species_by_common
    global _species_by_ebird, _species_by_inat_id
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
    _species_by_ebird = {}
    _species_by_inat_id = {}
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
        for name in rec.get("common_names", {}).values():
            if name:
                _species_by_common.setdefault(name.lower(), rec)
        ebird_code = rec.get("ebird_code", "")
        if ebird_code:
            _species_by_ebird[ebird_code.lower()] = rec
        inat_id = rec.get("inat_id")
        if inat_id:
            _species_by_inat_id[int(inat_id)] = rec
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

_image_base: Path = ROOT / "dist" / "images"
_image_sizes: dict[str, ImageSize] = {}
_image_qualities: dict[str, int] = {}
_dev_mode: bool = False


def _init_image_config(dev: bool = False):
    """Load image proxy settings from config."""
    global _image_base, _image_sizes, _image_qualities, _dev_mode
    _dev_mode = dev
    cfg = load_config()
    img = cfg.get("images", {})
    _image_base = ROOT / ("dev" if dev else "dist") / "images"
    _image_base.mkdir(parents=True, exist_ok=True)
    _image_sizes = {
        "thumb": ImageSize(img.get("thumb_width", 150), img.get("thumb_height", 100)),
        "medium": ImageSize(img.get("medium_width", 480), img.get("medium_height", 320)),
    }
    _image_qualities = {
        "thumb": img.get("thumb_quality", 20),
        "medium": img.get("medium_quality", 60),
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    if not _species_list:
        load_data()
    _init_image_config(dev=_dev_mode)
    templates.env.globals["quote_path"] = _quote_path
    yield


app = FastAPI(
    title=f"BirdNET Species Metadata API {_taxonomy_version}".strip(),
    description="Browse and query species metadata for BirdNET models.",
    version="1.0.0",
    lifespan=lifespan,
    root_path=_root_path,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.middleware("http")
async def normalize_root_path(request: FRequest, call_next):
    """Support proxies that either strip or preserve the configured prefix."""
    if _root_path:
        path = request.scope.get("path", "")
        prefix = _root_path
        if path == prefix or path.startswith(f"{prefix}/"):
            request.scope["root_path"] = prefix
            stripped = path[len(prefix):] or "/"
            request.scope["path"] = stripped
            raw_path = request.scope.get("raw_path")
            if raw_path:
                prefix_bytes = prefix.encode("utf-8")
                if raw_path == prefix_bytes or raw_path.startswith(prefix_bytes + b"/"):
                    request.scope["raw_path"] = raw_path[len(prefix_bytes):] or b"/"
    return await call_next(request)


def _template_context(request: FRequest, **context: Any) -> dict[str, Any]:
    """Inject common template values that must respect the deployment prefix."""
    return {
        "request": request,
        "base": request.scope.get("root_path") or _root_path,
        "taxonomy_version": _taxonomy_version,
        "site_title": _site_title,
        **context,
    }


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.get("/static/{file_path:path}", include_in_schema=False)
async def static_file(file_path: str):
    """Serve static files."""
    safe = Path(file_path).name
    local = STATIC_DIR / safe
    if not local.exists() or not local.is_file():
        raise HTTPException(404, "Not found")
    suffix = local.suffix.lower()
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".css": "text/css", ".js": "application/javascript",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return Response(
        content=local.read_bytes(),
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# Image proxy endpoint
# ---------------------------------------------------------------------------


def _source_image_url(rec: dict) -> str:
    """Return the upstream source image URL from either metadata shape."""
    image = rec.get("image")
    if isinstance(image, dict):
        src = image.get("src", "")
        if src:
            return src
    return rec.get("image_url", "")

@app.get("/api/image/{scientific_name:path}",
         tags=["Images"],
         responses={200: {"content": {"image/webp": {}}}})
async def image_proxy(scientific_name: str,
                      size: str = Query("medium", description="Image size: thumb or medium")):
    """Serve a species image as WebP.

    Sizes: thumb (150x100), medium (480x320).  Default is medium.
    Images are fetched from the original source, smart-cropped with YOLO,
    and saved to disk with meaningful filenames.  Subsequent requests are
    served directly from the saved file.
    """
    if size not in _image_sizes:
        raise HTTPException(400, f"Invalid size '{size}'. Use: thumb, medium")

    rec = _find_species(scientific_name)
    if not rec:
        # Unknown species — serve dummy fallback
        dummy = _image_base / size / "dummy.webp"
        if dummy.exists():
            return Response(
                content=dummy.read_bytes(),
                media_type="image/webp",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        raise HTTPException(404, "Species not found")

    source_url = _source_image_url(rec)
    if not source_url:
        # Serve dummy fallback image
        dummy = _image_base / size / "dummy.webp"
        if dummy.exists():
            return Response(
                content=dummy.read_bytes(),
                media_type="image/webp",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        raise HTTPException(404, "No image available for this species")

    sci = rec.get("scientific_name", "")
    common = rec.get("common_name", "")
    author = rec.get("image_author", "")
    crop_anchor = rec.get("image_crop_anchor")

    # Not on disk — fetch, crop, save with proper name
    image_dir = _image_base / size
    saved = save_species_image(
        url=source_url,
        scientific_name=sci,
        common_name=common,
        author=author,
        size=_image_sizes[size],
        image_dir=image_dir,
        quality=_image_qualities.get(size, 60),
        crop_anchor=crop_anchor,
    )
    if saved and saved.exists():
        return Response(
            content=saved.read_bytes(),
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Serve dummy fallback image
    dummy = _image_base / size / "dummy.webp"
    if dummy.exists():
        return Response(
            content=dummy.read_bytes(),
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    raise HTTPException(502, "Failed to fetch or convert source image")


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: FRequest, q: str = "", group: str = "",
               lang: str = "", sort: str = "", page: int = 1, per_page: int = 51):
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

    return templates.TemplateResponse("home.html", _template_context(
        request,
        species=page_results,
        query=q,
        group=group,
        lang=lang,
        sort=sort,
        locales=_all_locales,
        groups=groups,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    ))


@app.get("/species/{scientific_name:path}", response_class=HTMLResponse,
         include_in_schema=False)
async def species_page(request: FRequest, scientific_name: str):
    """Species detail page. Accepts scientific name, common name, eBird code, or iNat ID."""
    rec = _find_species(scientific_name)
    if not rec:
        raise HTTPException(status_code=404, detail="Species not found")

    # Redirect to canonical URL if accessed via alias
    canonical = rec.get("scientific_name", "")
    if canonical and scientific_name != canonical:
        return RedirectResponse(
            url=_with_root_path(f"/species/{_quote_path(canonical)}"),
            status_code=302,
        )

    return templates.TemplateResponse("species.html", _template_context(
        request,
        s=rec,
    ))


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
    fields.discard("image_url_cropped")
    fields.add("image")
    _ALL_FIELDS = fields


def _api_record(record: dict) -> dict:
    """Transform a raw data record for API output.

    Normalizes image data into a single ``image`` dict containing source and
    proxy URLs plus attribution metadata.
    """
    rec = dict(record)  # shallow copy

    sci = rec.get("scientific_name", "")
    image = rec.get("image")
    url = rec.pop("image_url", None)
    source = rec.pop("image_source", None)
    author = rec.pop("image_author", None)
    lic = rec.pop("image_license", None)
    rec.pop("image_url_cropped", None)

    if isinstance(image, dict):
        img = dict(image)
    elif url and sci:
        img = {
            "src": url,
            "thumb": _with_image_prefix(f"/api/image/{_quote_path(sci)}?size=thumb"),
            "medium": _with_image_prefix(f"/api/image/{_quote_path(sci)}?size=medium"),
        }
    else:
        img = None

    if img:
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
        result = [r for r in result if bool((r.get("image") or {}).get("medium") or r.get("image_url")) == want]

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

    Nested dicts are JSON-encoded in cells, except description excerpts,
    which are omitted from CSV exports.
    """
    if not records:
        return ""

    blocked = {"descriptions", "description"}

    if fields:
        columns = [f.strip() for f in fields.split(",") if f.strip() and f.strip() not in blocked]
    else:
        # Deterministic column order: fixed columns first, rest sorted
        priority = [
            "scientific_name", "common_name", "taxon_group",
            "observations_count", "inat_id", "ebird_code",
        ]
        all_keys: set[str] = set()
        for r in records:
            all_keys.update(r.keys())
        all_keys -= blocked
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
    groups = Counter(r.get("taxon_group", "unknown") for r in _species_list)
    return {
        "taxonomy_version": _taxonomy_version,
        "total_species": len(_species_list),
        "groups": dict(sorted(groups.items())),
        "locales": [code for code, _ in _all_locales],
    }


@app.get("/api/groups", tags=["API"], response_model=list[GroupCount])
async def api_groups():
    """List available taxon groups with species counts."""
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
        "taxonomy_version": _taxonomy_version,
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
        "taxonomy_version": _taxonomy_version,
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
    """Get full metadata for one species by scientific name, common name, eBird code, or iNat ID."""
    rec = _find_species(scientific_name)
    if not rec:
        raise HTTPException(status_code=404, detail="Species not found")
    out = _project(rec, fields, exclude, locale)
    out["taxonomy_version"] = _taxonomy_version
    return out


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
