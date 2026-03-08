# BirdNET Species Metadata

Species metadata and taxonomy for BirdNET models and apps.

## Overview

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
| 5. Claude | `python -m utils.claude` | `raw_data/claude_data.json` |

Steps 1–4 collect raw data from external sources. Step 5 uses Claude to generate English descriptions and translate them to all configured locales. All scripts are incremental — rerunning skips already-processed species. Use `--limit N` to cap the number of new items per run, or `--dry-run` to preview without writing.

### Merge & Release

Once raw data is collected, merge everything into the final artifact:

```bash
python merge.py            # → dist/species_metadata.{json,csv,zip}
python merge.py --dev      # → dev/  (for local iteration, no zip needed)
python merge.py --no-zip   # write json + csv without zipping
```

The `raw_data/`, `dev/`, and `dist/` directories are all gitignored. Zip archives from `dist/` are attached to GitHub releases.

## Data Sources

- **[iNaturalist](https://www.inaturalist.org)** — Taxonomy, common names, observation counts, and photos via the public API. Data licensed under various Creative Commons licenses by individual contributors.
- **[eBird](https://ebird.org)** — Species descriptions and images from the Cornell Lab of Ornithology. Species codes from the eBird/Clements taxonomy.
- **[Wikipedia](https://www.wikipedia.org)** — English summaries and localized article links via the REST and MediaWiki APIs. Content available under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- **[AviList](https://www.avilist.org)** — The Global Avian Checklist (v2025). AviList Core Team, 2025. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). doi:[10.2173/avilist.v2025](https://doi.org/10.2173/avilist.v2025).
- **[Claude](https://www.anthropic.com/claude)** (Anthropic) — AI-generated species descriptions and translations.
