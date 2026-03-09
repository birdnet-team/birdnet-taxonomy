<p align="center">
  <img src="birdnet-logo-circle.png" width="250" alt="BirdNET Logo">
</p>

<h1 align="center">BirdNET Species Metadata</h1>

Pipeline for collecting and merging species metadata from multiple sources. Covers birds, mammals, insects, reptiles, and amphibians. All configuration (locales, taxon groups, API settings) lives in `config.yml`.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For Claude-generated descriptions, add your API key to a `.env` file:

```
ANTHROPIC_API_KEY=...
```

## Project Structure

```
config.py               # Configuration loader (reads config.yml)
config.yml              # All settings: locales, taxon groups, API params
utils/
    images.py           # Image pipeline (download, YOLO crop, WebP convert)
collectors/
    _common.py          # Shared utilities (rate limiter, JSON I/O, shutdown)
    avilist.py          # Download AviList checklist (XLSX → CSV)
    inat.py             # iNaturalist taxa paginator
    ebird.py            # eBird page scraper (descriptions, images)
    wikipedia.py        # Wikipedia summaries, langlinks, image licenses
    claude.py           # Claude API (descriptions + translations)
    images.py           # Batch image downloader (smart-crop + save)
build/
    metadata.py         # Cross-reference all sources → final metadata
web/
    server.py           # FastAPI server (HTML + REST API + image proxy)
    templates/          # Jinja2 templates (home, species detail, base)
    static/             # Logo and static assets
```

## Pipeline

Run collectors in order — later steps depend on earlier output. All scripts are incremental (rerunning skips already-processed species). Use `--limit N` to cap new items per run, or `--dry-run` to preview.

| Step | Command | Output |
|------|---------|--------|
| 1. AviList | `python -m collectors.avilist` | `raw_data/AviList-*.csv` |
| 2. iNaturalist | `python -m collectors.inat` | `raw_data/inat_data.json` |
| 3. eBird | `python -m collectors.ebird` | `raw_data/ebird_data.json` |
| 4. Wikipedia | `python -m collectors.wikipedia` | `raw_data/wikipedia_data.json` |
| 5. Claude (optional) | `python -m collectors.claude` | `raw_data/claude_data.json` |
| 6. Images (optional) | `python -m collectors.images` | `dist/images/*.webp` |

Steps 1–2 collect source taxonomy data. Steps 3–5 enrich species with descriptions, images, and translations. Step 5 optionally uses Claude to generate polished English descriptions and translate them to configured locales. Step 6 batch-downloads species images as named WebP files with content-aware smart cropping (YOLOv8-nano animal detection).

### Build

Once raw data is collected, build the final metadata file. This runs two phases:

1. **Taxonomy** — cross-references iNaturalist, AviList, and Wikidata to build a canonical species list with eBird codes, common names (60+ locales via eBird + Wikidata), external identifiers (GBIF, NCBI, Avibase, BirdLife), and default images (iNat → Wikimedia Commons → eBird).
2. **Merge** — enriches each species with a single description (Claude > Wikipedia > eBird priority) and writes the final output.

```bash
python -m build.metadata              # full rebuild → dist/species_metadata.{json,csv,zip}
python -m build.metadata --merge-only # skip taxonomy, re-merge only
python -m build.metadata --dev        # write to dev/ instead of dist/
python -m build.metadata --no-zip     # skip zip archive
python -m build.metadata --dry-run    # show stats without writing
```

The `raw_data/`, `dev/`, and `dist/` directories are all gitignored. Zip archives from `dist/` are attached to GitHub releases.

### Web Server

Browse and search the dataset through a web UI and REST API. Species images are served through a built-in proxy that fetches from the original source, converts to WebP, smart-crops to 3:2 using YOLOv8-nano animal detection, and saves as named files (`<sci>_<common>_<author>_<size>.webp`). Pre-downloaded images from the collector are served directly.

```bash
python -m web.server              # serve from dist/species_metadata.json
python -m web.server --dev        # serve from dev/species_metadata.json
python -m web.server --port 3000  # custom port
```

Or with hot-reload during development:

```bash
uvicorn web.server:app --reload
```

| Route | Description |
|-------|-------------|
| `/` | Home page — search, browse, filter by taxon group |
| `/species/{name}` | Species detail page (HTML) |
| `/api/image/{name}/{size}` | Image proxy — `thumb`, `medium`, `large` (WebP) |
| `/api/species` | List species (JSON), supports `?group=`, `?page=` |
| `/api/species/{name}` | Single species detail (JSON) |
| `/api/search?q=` | Search species by name |
| `/api/groups` | List taxon groups with counts |
| `/api/stats` | Dataset statistics |
| `/docs` | Interactive API docs (Swagger UI) |

## Data Sources

- **[iNaturalist](https://www.inaturalist.org)** — Taxonomy, common names, observation counts, and photos via the public API. Data licensed under various Creative Commons licenses by individual contributors.
- **[eBird](https://ebird.org)** — Species descriptions and images from the Cornell Lab of Ornithology. Species codes from the eBird/Clements taxonomy.
- **[Wikipedia](https://www.wikipedia.org)** — English summaries and localized article links via the REST and MediaWiki APIs. Content available under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- **[AviList](https://www.avilist.org)** — The Global Avian Checklist (v2025). AviList Core Team, 2025. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). doi:[10.2173/avilist.v2025](https://doi.org/10.2173/avilist.v2025).
- **[Claude](https://www.anthropic.com/claude)** (Anthropic) — AI-generated species descriptions and translations.

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
