<p align="center">
  <img src="birdnet-logo-circle.png" width="250" alt="BirdNET Logo">
</p>

<h1 align="center">BirdNET+ Taxonomy</h1>

<p align="center">
    <a href="https://birdnet.cornell.edu/taxonomy/api/stats"><img alt="Taxonomy version" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fbirdnet.cornell.edu%2Ftaxonomy%2Fapi%2Fstats&query=%24.taxonomy_version&label=taxonomy&color=ff6b00"></a>
    <a href="https://birdnet.cornell.edu/taxonomy/api/stats"><img alt="Species count" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fbirdnet.cornell.edu%2Ftaxonomy%2Fapi%2Fstats&query=%24.total_species&label=species&color=00c853"></a>
    <a href="https://birdnet.cornell.edu/taxonomy/docs"><img alt="API docs" src="https://img.shields.io/badge/API-docs-00a6fb"></a>
    <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-ff2d55"></a>
</p>

Pipeline for collecting and merging species metadata from multiple sources. Covers birds, mammals, insects, reptiles, and amphibians. All configuration (locales, taxon groups, API settings) lives in `config.yml`.

## Table of Contents

- [Setup](#setup)
- [Contributing](#contributing)
- [Project Structure](#project-structure)
- [Taxon Groups](#taxon-groups)
- [Pipeline](#pipeline)
- [Step 1 - AviList](#step-1--avilist)
- [Step 2 - iNaturalist](#step-2--inaturalist)
- [Step 3 - eBird](#step-3--ebird)
- [Step 4 - Wikidata](#step-4--wikidata)
- [Step 5 - Wikipedia](#step-5--wikipedia)
- [Step 6 - Claude](#step-6--claude)
- [Step 7 - Images](#step-7--images)
- [Step 8 - Build](#step-8--build)
- [Web Server](#web-server)
- [Data Sources](#data-sources)
- [License](#license)
- [Funding](#funding)
- [Partners](#partners)

## Setup

```bash
git clone https://github.com/birdnet-team/birdnet-taxonomy.git
cd birdnet-taxonomy
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For Claude translations and shortening, add your API key to a `.env` file:

```
ANTHROPIC_API_KEY=...
```

For sub-path deployment behind a reverse proxy (e.g. `https://example.com/taxonomy/`),
add the URL prefix:

```
ROOT_PATH=/taxonomy
HOST_NAME=https://birdnet.cornell.edu
```

The web app will then generate links under that prefix and accepts deployments where
the reverse proxy either preserves the prefix or strips it before forwarding.

`HOST_NAME` is used for absolute image URLs in built metadata and API responses. With the example above, JSON image URLs and CSV `image_url` values are emitted under `https://birdnet.cornell.edu/taxonomy/api/image/...`.


## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution workflow and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

Community-maintained manual overrides live in `overrides/species_overrides.csv` and are applied during `python -m build.metadata`.

These overrides are persistent, tracked in Git, and always take precedence over fetched image data.

If an override changes the effective image URL or crop anchor, the image pipeline regenerates the same named `.webp` file in place. Cache freshness is tracked separately in per-image JSON sidecars under `dev/images/*/.state/` or `dist/images/*/.state/`.

Supported columns:

- `scientific_name` — required, exact species name
- `image_url`, `image_author`, `image_license`, `image_source` — optional, but if any one is set then all four are required and replace the fetched image metadata
- `image_crop_anchor` — optional 3x3 crop anchor (`1`..`9`), where `5` is center crop, `3` is top-right, `7` is bottom-left, etc.
- `source_url`, `notes` — optional review context for contributors

Build validation is strict. The build fails on duplicate species rows, invalid crop anchors, partial image overrides, or species names not present in the taxonomy.

Current manual override support covers image replacement and manual crop anchoring. The crop anchor bypasses smart crop and uses a fixed 3×3 grid position in the final image pipeline.

## Project Structure

```
config.py                  # Configuration helpers for config.yml access
config.yml                 # Project settings: taxonomy version, groups, filters, API params
requirements.txt           # Python dependencies
build/
    metadata.py            # Merge collected sources into final metadata outputs
collectors/
    _common.py             # Shared collector utilities (cache, JSON I/O, shutdown)
    avilist.py             # Download and normalize AviList taxonomy input
    claude.py              # Claude enrichment for shortened/translated descriptions
    ebird.py               # eBird names and localized common-name collection
    images.py              # Batch image generation for dev/dist outputs
    inat.py                # iNaturalist taxa, sounds, and observation-photo fallback
    wikidata.py            # Wikidata licenses and cross-reference metadata
    wikipedia.py           # Wikipedia summaries, langlinks, and image metadata
dev/                       # Development metadata snapshots and local build artifacts
dist/                      # Published metadata and generated site image assets
overrides/
    species_overrides.csv  # Git-tracked manual image and crop overrides
raw_data/                  # Cached upstream source payloads and intermediate collector output
utils/
    images.py              # Image download, crop, cache-state, and WebP helpers
web/
    server.py              # FastAPI app serving HTML, REST API, and image endpoints
    static/                # Static web assets
    templates/             # Jinja2 templates for home and species pages
```

## Taxon Groups

Configured in `config.yml`. Birds include all species; other groups are limited to species with sound observations on iNaturalist.

| Group | iNat Taxon ID | Mode | Min sound observations |
|-------|---------------|------|------------------------|
| Aves | 3 | All species | — |
| Mammalia | 40151 | Sounds only | 1 |
| Insecta | 47158 | Sounds only | 5 |
| Reptilia | 26036 | Sounds only | 1 |
| Amphibia | 20978 | Sounds only | 5 |

## Pipeline

Run collectors in order — later steps depend on earlier output. All scripts are incremental (rerunning skips already-processed species). Use `--limit N` to cap new items per run, or `--dry-run` to preview.

| Step | Command | Output |
|------|---------|--------|
| 1. AviList | `python -m collectors.avilist` | `raw_data/AviList-*.csv` |
| 2. iNaturalist | `python -m collectors.inat` | `raw_data/inat_data.json` |
| 3. eBird | `python -m collectors.ebird` | `raw_data/ebird_data.json`, `raw_data/ebird_names.json` |
| 4. Wikidata | `python -m collectors.wikidata` | `raw_data/wikidata_data.json` |
| 5. Wikipedia | `python -m collectors.wikipedia` | `raw_data/wikipedia_data.json` |
| 6. Claude (optional) | `python -m collectors.claude` | `raw_data/claude_data.json` |
| 7. Images (optional) | `python -m collectors.images` | `dist/images/` (`--dev` → `dev/images/`) |
| 8. Build | `python -m build.metadata` | `dist/species_metadata.{json,csv,zip}` |

Steps 1–2 collect taxonomy. Steps 3–4 enrich species with eBird descriptions, common names, external identifiers, and Wikidata images. Step 5 fetches localized Wikipedia summaries. Step 6 uses Claude to shorten excessively long extracts and translate missing locales. Step 7 downloads species images. Step 8 merges everything into the final output — no API calls, purely offline.

### Step 1 — AviList

Downloads the AviList Global Avian Checklist (XLSX), converts to CSV. Provides authoritative bird taxonomy and AviList IDs.

### Step 2 — iNaturalist

Paginates the iNat taxa API to fetch all species for each taxon group. For birds, fetches all species. For other groups, queries the iNat sounds API to find species with audio observations meeting the `min_observations` threshold. Collects taxonomy, common names (all locales when `all_names: true`), observation counts, default photos, and Wikipedia URLs.

After group fetching, runs an **observation photo fallback** phase: for any species whose default taxon photo is missing or not CC-licensed, queries the iNat observations API for a CC-licensed photo from a research-grade observation (sorted by community votes). The result is stored in the `obs_photo` field, and unsuccessful lookups are cached in `inat_data.json` so later runs do not repeat the same slow checks.

```bash
python -m collectors.inat                   # fetch all groups + obs photos
python -m collectors.inat --group Aves      # fetch only birds
python -m collectors.inat --obs-photos-only # only run observation photo fallback
python -m collectors.inat --skip-obs-photos # skip observation photo fallback
python -m collectors.inat --refresh-obs-photos # recheck species cached as no obs photo
python -m collectors.inat --limit 100       # cap new species per group
python -m collectors.inat --dry-run         # preview without fetching
```

### Step 3 — eBird

Collects eBird species data in two phases:

- **Phase 1 — Scraper:** Scrapes eBird species pages for English descriptions (`og:description`) and Macaulay Library images (`og:image`). Parallel fetching with configurable workers (default 4) and rate limiting (default 5 rps). Only for bird species with an eBird code.
- **Phase 2 — Common names:** Downloads the eBird taxonomy CSV for 62 locales to collect common names in all available languages. Each locale download is cached to avoid re-fetching.

```bash
python -m collectors.ebird                  # run both phases
python -m collectors.ebird --names-only     # only download common names (Phase 2)
python -m collectors.ebird --skip-names     # skip common names, scrape only
python -m collectors.ebird --limit 100      # cap new species for scraping
python -m collectors.ebird --workers 8      # parallel scrapers
python -m collectors.ebird --rps 10         # custom rate limit
python -m collectors.ebird --dry-run        # preview without fetching
```

### Step 4 — Wikidata

Fetches species identifiers, common name labels, and images from Wikidata and Wikimedia Commons via SPARQL queries.

- **Phase 1 — eBird codes:** Resolves eBird species codes (P3444) for species not yet matched via AviList, using scientific name (P225) and iNat taxon ID (P3151) as lookup keys.
- **Phase 2 — Identifiers:** Fetches external identifiers: GBIF (P846), NCBI (P685), Avibase (P2426), BirdLife (P5257).
- **Phase 3 — Labels:** Fetches `rdfs:label` common names in all available languages.
- **Phase 4 — Images:** Fetches Wikidata P18 images and checks Wikimedia Commons licenses (CC, PD, GFDL).

```bash
python -m collectors.wikidata              # fetch all phases
python -m collectors.wikidata --no-cache   # bypass request cache
python -m collectors.wikidata --dry-run    # show species count without querying
```

### Step 5 — Wikipedia

Fetches multilingual Wikipedia data in four phases:

- **Phase 1 — English Wikipedia:** Batch-fetches extracts, langlinks, page images, and Wikidata descriptions for each species' Wikipedia article. Up to 50 titles per request. Includes a search fallback for titles not found in batch results.
- **Phase 1b — Extract backfill:** Scans existing data for species that have an English Wikipedia URL but are missing the English extract (can happen due to API glitches during bulk fetching). Re-fetches just the extracts for those species with redirect resolution.
- **Phase 2 — Locale extracts:** For each target language (20 configured locales), batch-fetches intro extracts from the corresponding Wikipedia. Runs locales concurrently with a thread pool. Skips species that already have extracts for a given locale.
- **Phase 3 — Image licenses:** Batch-fetches license metadata (artist, license, license URL) from Wikimedia Commons for all page images found in Phase 1.

Rate-limited (default 25 rps), with exponential backoff on 429s and server errors. All phases save incrementally.

**Wikipedia locales:** en, de, fr, es, pt, it, nl, pl, sv, da, no, fi, cs, zh, ru, ar, ja, ko, tr, sw

```bash
python -m collectors.wikipedia              # fetch all
python -m collectors.wikipedia --limit 100  # cap at 100 new species
python -m collectors.wikipedia --refetch    # re-fetch species with few locale extracts
python -m collectors.wikipedia --rps 10     # custom rate limit
python -m collectors.wikipedia --dry-run    # preview without fetching
```

### Step 6 — Claude

Uses the Claude API (Sonnet 4) for two tasks on existing Wikipedia extracts — no content is generated from scratch:

- **Phase 1 — Shorten:** Finds extracts exceeding `max_extract_words` (default 500 words) and asks Claude to condense them to `target_words` (default 150 words), preserving the original language and key facts (appearance, habitat, range, behaviour).
- **Phase 2 — Translate:** Finds species that have an English extract but are missing translations for Claude's target locales. Sends the English text to Claude for translation into all missing locales at once.

Claude's output is stored separately in `claude_data.json` and overlaid on top of Wikipedia extracts during the build step. Claude only fills gaps — it never overwrites existing Wikipedia extracts for a locale.

Translation batches are grouped by the exact set of missing locales for each species, then packed by source-text size. This keeps prompts smaller and makes parallel API calls practical on large repair runs.

**Claude locales:** en, de, fr, es, pt, it, nl, zh, ru, ar (subset of Wikipedia locales)

```bash
python -m collectors.claude                   # run both phases
python -m collectors.claude --shorten-only    # only shorten long extracts
python -m collectors.claude --translate-only  # only translate missing locales
python -m collectors.claude --batch-size 12   # max species per API call
python -m collectors.claude --workers 4       # parallel translation workers
python -m collectors.claude --char-budget 12000  # source chars per API call
python -m collectors.claude --limit 50        # cap total work items
python -m collectors.claude --dry-run         # preview without API calls
```

### Step 7 — Images

Batch-downloads species images as WebP files with content-aware smart cropping. Each species gets two sizes stored in subdirectories:

| Size | Dimensions | Quality | Path |
|------|-----------|---------|------|
| thumb | 150×100 | 40 | `images/thumb/` |
| medium | 480×320 | 60 | `images/medium/` |

**Filename format:** `<scientific name>_<common name>_<author>.webp`

**Cache state:** each generated image has a sidecar JSON file in `.state/` storing the effective source URL and optional manual crop anchor. This keeps filenames stable while still forcing regeneration when an override changes.

**Smart cropping** uses YOLOv8-nano (ONNX) for animal detection. The model prefers COCO animal classes (bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe) and centers the crop on the detected subject. For tall subjects (e.g. a woodpecker on a trunk), the crop prefers the upper portion to keep the head visible. Falls back to center-crop if no animal is detected or if the ONNX runtime is unavailable.

When `image_crop_anchor` is set in `overrides/species_overrides.csv`, smart crop is bypassed and a fixed 3×3 anchor crop is used instead.

**Dummy images:** On startup, generates a grayscale dummy WebP (neutral gray background with centered BirdNET logo) for each size. When a download or conversion fails, the dummy is copied as the species' named file so every species has an image file.

The collector also prunes obsolete cached `.webp` files and stale `.state` metadata left behind by older naming schemes or old fallback files.

```bash
python -m collectors.images              # download to dist/images/
python -m collectors.images --dev        # download to dev/images/
python -m collectors.images --workers 8  # parallel downloaders
python -m collectors.images --limit 100  # cap at 100 species
python -m collectors.images --dry-run    # preview without downloading
```

### Step 8 — Build

Merges all pre-collected data into the final metadata file. Runs purely offline — no API calls. Two phases:

**Taxonomy phase:**
1. Cross-references iNaturalist, AviList, and Wikidata to build a canonical species list
2. Resolves eBird codes from AviList and pre-collected Wikidata data
3. Loads external identifiers from Wikidata (GBIF, NCBI, Avibase, BirdLife)
4. Collects common names from eBird (62 locales) and Wikidata labels
5. Selects the best image for each species through a priority chain:
   - **iNaturalist taxon photo** — default photo if CC-licensed
   - **Macaulay Library** — eBird image (source tagged as "Macaulay Library ML{asset_id}")
   - **Wikimedia Commons** — Wikidata P18 image if CC/PD/GFDL licensed
   - **iNaturalist observation photo** — CC-licensed photo from research-grade observations (last resort)

**Merge phase:**
Assembles per-species descriptions from multiple sources with the following priority:
- **Base layer — Wikipedia:** English extract plus all locale extracts and Wikipedia URLs
- **Fallback — eBird:** English description only, used when no Wikipedia article exists
- **Claude overlay:** For each locale Claude provides, replaces the description for that locale. Claude locales are tracked in the `claude_locales` field

The effective priority is **Claude > Wikipedia > eBird**, applied per-locale.

The JSON output contains full multilingual descriptions. The CSV output is a lighter export and does not include description excerpts.

Image fields in the final metadata:

- JSON metadata stores an `image` object with `src`, `thumb`, and `medium`
- CSV metadata flattens this to a single `image_url` column containing the local served medium image URL

```bash
python -m build.metadata              # full rebuild → dist/species_metadata.{json,csv,zip}
python -m build.metadata --merge-only # skip taxonomy, re-merge only
python -m build.metadata --dev        # write to dev/ instead of dist/
python -m build.metadata --no-zip     # skip zip archive
python -m build.metadata --dry-run    # show stats without writing
```

The `raw_data/`, `dev/`, and `dist/` directories are all gitignored. Zip archives from `dist/` are attached to GitHub releases.

### Web Server

Browse and search the dataset through a web UI and REST API.

```bash
python -m web.server              # serve from dist/species_metadata.json
python -m web.server --dev        # serve from dev/species_metadata.json
python -m web.server --port 8888  # custom port
```

Or with hot-reload during development:

```bash
uvicorn web.server:app --reload
```

**Species lookup** supports multiple identifier types. The `/species/{name}` and `/api/species/{name}` endpoints accept any of:
- Scientific name (e.g., `Turdus merula`)
- Common name in any locale (e.g., `Amsel`, `Merle noir`)
- eBird species code (e.g., `eurblk1`)
- iNaturalist taxon ID (e.g., `12727`)

HTML species pages redirect to the canonical scientific name URL when accessed via an alias.

**Image proxy** (`/api/image/{name}?size=thumb|medium`) serves species images with on-demand downloading, smart cropping, and caching. Returns a dummy image (BirdNET logo on gray background) for unknown species or failed downloads. All images cached with 24-hour `Cache-Control` headers.

| Route | Description |
|-------|-------------|
| `/` | Home page — search, browse, filter by taxon group |
| `/species/{name}` | Species detail page (HTML) |
| `/api/image/{name}?size=` | Image proxy — `thumb` (150×100), `medium` (480×320, default) |
| `/api/species` | List species (JSON/CSV) with filtering, sorting, field selection; CSV omits description excerpts |
| `/api/species/{name}` | Single species detail (JSON) with field selection |
| `/api/search?q=` | Search species by name with full query options |
| `/api/fields` | List all available field names |
| `/api/groups` | List taxon groups with counts |
| `/api/stats` | Dataset statistics |
| `/docs` | Interactive API docs (Swagger UI) |

#### API Query Parameters

All list/search endpoints (`/api/species`, `/api/search`) support these parameters:

| Parameter | Example | Description |
|-----------|---------|-------------|
| `fields` | `?fields=scientific_name,common_name` | Return only specified fields (comma-separated) |
| `exclude` | `?exclude=common_names,descriptions` | Return all fields except these |
| `locale` | `?locale=en,de,fr` | Filter `common_names` and `descriptions` to specific locales |
| `sort` | `?sort=-observations_count` | Sort by field; prefix `-` for descending |
| `group` | `?group=Aves` | Filter by taxon group |
| `has_image` | `?has_image=true` | Filter species with/without images |
| `has_description` | `?has_description=true` | Filter species with/without English description |
| `description_source` | `?description_source=claude,wikipedia` | Filter by description source |
| `min_observations` | `?min_observations=10000` | Minimum iNaturalist observation count |
| `max_observations` | `?max_observations=50000` | Maximum iNaturalist observation count |
| `format` | `?format=csv` | Response format — `json` (default) or `csv` |
| `page` | `?page=2` | Page number (default 1) |
| `per_page` | `?per_page=100` | Results per page (1–500, default 50) |

The detail endpoint (`/api/species/{name}`) supports `fields`, `exclude`, and `locale`.

**Examples:**

```bash
# Top 10 most observed birds with images
curl '/api/species?group=Aves&has_image=true&sort=-observations_count&per_page=10&fields=scientific_name,common_name,observations_count'

# German and French names/descriptions for a species
curl '/api/species/Anas%20platyrhynchos?locale=de,fr&fields=scientific_name,common_names,descriptions'

# Export all mammals as CSV
curl '/api/species?group=Mammalia&per_page=500&format=csv' > mammals.csv

# Search with field selection
curl '/api/search?q=eagle&fields=scientific_name,common_name&per_page=20'

# Look up a species by eBird code
curl '/api/species/eurblk1'
```

## Data Sources

- **[iNaturalist](https://www.inaturalist.org)** — Taxonomy, common names, observation counts, and photos via the public API. Data licensed under various Creative Commons licenses by individual contributors.
- **[eBird](https://ebird.org)** — Species descriptions, images, and common names (62 locales) from the Cornell Lab of Ornithology. Species codes from the eBird/Clements taxonomy.
- **[Wikidata](https://www.wikidata.org)** — External identifiers (GBIF, NCBI, Avibase, BirdLife), eBird codes, common name labels, and P18 images via SPARQL. Data available under [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
- **[Wikipedia](https://www.wikipedia.org)** — English summaries and localized article links via the REST and MediaWiki APIs. Content available under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- **[AviList](https://www.avilist.org)** — The Global Avian Checklist (v2025). AviList Core Team, 2025. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). doi:[10.2173/avilist.v2025](https://doi.org/10.2173/avilist.v2025).
- **[Claude](https://www.anthropic.com/claude)** (Anthropic) — AI-powered translation of Wikipedia extracts to missing locales and shortening of excessively long extracts.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Funding

Our work in the K. Lisa Yang Center for Conservation Bioacoustics is made possible by the generosity of K. Lisa Yang to advance innovative conservation technologies to inspire and inform the conservation of wildlife and habitats.

The development of BirdNET is supported by the German Federal Ministry of Research, Technology and Space (FKZ 01|S22072), the German Federal Ministry for the Environment, Climate Action, Nature Conservation and Nuclear Safety (FKZ 67KI31040E), the German Federal Ministry of Economic Affairs and Energy (FKZ 16KN095550), the Deutsche Bundesstiftung Umwelt (project 39263/01) and the European Social Fund.

## Partners

BirdNET is a joint effort of partners from academia and industry.
Without these partnerships, this project would not have been possible.
Thank you!

![Logos of all partners](https://tuc.cloud/index.php/s/KSdWfX5CnSRpRgQ/download/box_logos.png)
