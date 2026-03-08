#!/usr/bin/env python3
"""
FastAPI web server for browsing and querying species metadata.

Provides:
  - HTML pages: home/search, species detail
  - REST API: /api/species, /api/species/{name}, /api/search, /api/stats
  - Auto-generated docs at /docs (Swagger) and /redoc

Usage:
    python server.py                    # start on port 8000
    python server.py --port 3000        # custom port
    python server.py --dev              # load from dev/ instead of dist/
    uvicorn server:app --reload         # development with auto-reload
"""

import argparse
import json
import re
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from utils.config import LOCALE_NAMES, get_locales

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
IMAGES_DIR = ROOT / "images"

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

_species_list: list[dict] = []
_species_by_name: dict[str, dict] = {}
_species_by_common: dict[str, dict] = {}
_search_index: list[tuple[str, str, dict]] = []  # (lower_sci, lower_common, record)


def load_data(dev: bool = False):
    """Load species_metadata.json into memory."""
    global _species_list, _species_by_name, _species_by_common, _search_index

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
        # Also include all locale common names
        for name in rec.get("common_names", {}).values():
            search_text += " " + _normalise(name)
        _search_index.append((sci.lower(), search_text, rec))


def _normalise(text: str) -> str:
    """Normalise text for search: lowercase, strip accents."""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    if not _species_list:
        load_data()
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

# Serve local images if directory exists
if IMAGES_DIR.exists():
    app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request, q: str = "", group: str = "",
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

    # Available locales from config (sorted by display name, exclude 'en')
    config_locales = get_locales()
    locales = [(code, LOCALE_NAMES.get(code, code)) for code in config_locales if code != "en"]
    locales.sort(key=lambda x: x[1])

    return templates.TemplateResponse("home.html", {
        "request": request,
        "species": page_results,
        "query": q,
        "group": group,
        "lang": lang,
        "sort": sort,
        "locales": locales,
        "groups": groups,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


@app.get("/species/{scientific_name:path}", response_class=HTMLResponse,
         include_in_schema=False)
async def species_page(request: Request, scientific_name: str):
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
        "locales": list(set(
            loc for r in _species_list
            for loc in r.get("common_names", {}).keys()
        )),
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
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
