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

## Pipeline

Each step has its own script. Run them in order — later steps depend on earlier output.

| Step | Command | Output |
|------|---------|--------|
| 1. AviList | `python -m utils.avilist` | `raw_data/AviList-*.csv` |
| 2. iNaturalist | `python -m utils.inat` | `raw_data/inat_data.json` |
| 3. eBird | `python -m utils.ebird` | `raw_data/ebird_data.json` |
| 4. Wikipedia | `python -m utils.wikipedia` | `raw_data/wikipedia_data.json` |
| 5. Claude (optional) | `python -m utils.claude` | `raw_data/claude_data.json` |

Steps 1–4 collect raw data from external sources. Step 5 optionally uses Claude to generate polished English descriptions and translate them to all configured locales. Without step 5, merge uses Wikipedia extracts (per-locale where available, English as fallback). All scripts are incremental — rerunning skips already-processed species. Use `--limit N` to cap the number of new items per run, or `--dry-run` to preview without writing.

### Merge & Release

Once raw data is collected, merge everything into the final artifact:

```bash
python merge.py            # → dist/species_metadata.{json,csv,zip}
python merge.py --dev      # → dev/  (for local iteration, no zip needed)
python merge.py --no-zip   # write json + csv without zipping
```

The `raw_data/`, `dev/`, `dist/`, and `images/` directories are all gitignored. Zip archives from `dist/` are attached to GitHub releases.

### Images (optional)

Download species images, convert to WebP thumbnails and medium-size crops (3:2 aspect ratio, center-cropped):

```bash
python -m utils.images             # download all, both iNat + eBird sources
python -m utils.images --source inat --limit 100
python -m utils.images --dry-run
```

Images are saved to `images/` (gitignored). Filename format: `<sci_name>_<common_name>_<author>_<thumb|medium>.webp`.

### Web Server

Browse and search the merged dataset through a web UI and REST API:

```bash
python server.py           # serve from dist/species_metadata.json
python server.py --dev     # serve from dev/species_metadata.json
```

Or with hot-reload during development:

```bash
uvicorn server:app --reload
```

| Route | Description |
|-------|-------------|
| `/` | Home page — search, browse, filter by taxon group |
| `/species/{name}` | Species detail page (HTML) |
| `/api/species` | List species (JSON), supports `?q=`, `?group=`, `?page=` |
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
