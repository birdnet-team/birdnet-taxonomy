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

Pipeline for collecting and merging species metadata from multiple sources. Covers birds, mammals, insects, reptiles, amphibians, and configured non-species sound classes. All configuration (locales, taxon groups, sound classes, API settings) lives in `config.yml`.

Current working taxonomy version: `v0.4-Oct2026`.

**Important note: While still in development (i.e., all releases prior to 1.0), species IDs may change. We will freeze species IDs upon official release.**

Download the most recent version here: https://birdnet.cornell.edu/taxonomy/download

## Table of Contents

- [Setup](#setup)
- [Contributing](#contributing)
- [Project Structure](#project-structure)
- [Taxon Groups](#taxon-groups)
- [Taxonomy Rules](#taxonomy-rules)
- [Pipeline](#pipeline)
- [Step 1 - AviList](#step-1--avilist)
- [Step 2 - iNaturalist](#step-2--inaturalist)
- [Step 3 - eBird](#step-3--ebird)
- [Step 4 - Wikidata](#step-4--wikidata)
- [Step 5 - Wikipedia](#step-5--wikipedia)
- [Step 6 - Macaulay Library](#step-6--macaulay-library)
- [Step 7 - Xeno-Canto](#step-7--xeno-canto)
- [Step 8 - observation.org](#step-8--observationorg)
- [Step 9 - Sound Classes](#step-9--sound-classes)
- [Step 10 - LLM Translation](#step-10--llm-translation)
- [Step 11 - Images](#step-11--images)
- [Step 12 - Build](#step-12--build)
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

For LLM-based translations, Xeno-Canto lookups, and optional public
translation-service keys, add API keys to a `.env` file:

```
# LLM translation (Step 10) — add whichever you have; Gemini is preferred
GEMINI_API_KEY=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
# Xeno-Canto (Step 7)
XC_API_KEY=...
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
config.yml                 # Project settings: taxonomy version, groups, sound classes, filters, API params
requirements.txt           # Python dependencies
bn_ids.json                # Persistent BirdNET species ID registry (git-tracked)
build/
    metadata.py            # Merge collected sources into final metadata outputs
collectors/
    _common.py             # Shared collector utilities (cache, JSON I/O, shutdown)
    avilist.py             # Download and normalize AviList taxonomy input
    translate.py           # Optional LLM translation: shorten/translate Wikipedia extracts
    ebird.py               # eBird names and localized common-name collection
    images.py              # Batch image generation for dev/dist outputs
    inat.py                # iNaturalist taxa, sounds, and observation-photo fallback
    macaulay.py            # Macaulay Library taxon code discovery
    wikidata.py            # Wikidata licenses and cross-reference metadata
    wikipedia.py           # Wikipedia summaries, langlinks, and image metadata
    xenocanto.py           # Xeno-Canto scientific name mapping
    observationorg.py      # observation.org species ID mapping
    sound_classes.py       # Configured anthropogenic/geophony sound classes
dev/                       # Development metadata snapshots and local build artifacts
dist/                      # Published metadata and generated site image assets
overrides/
    bird_splits.csv       # Git-tracked birds kept separate from an AviList lump
    priority_species.csv  # Git-tracked reviewed taxonomy additions
    species_aliases.csv   # Git-tracked manual scientific-name aliases
    species_overrides.csv  # Git-tracked manual image and crop overrides
raw_data/                  # Cached upstream source payloads and intermediate collector output
utils/
    audit_descriptions.py  # Focused description coverage report
    audit_metadata.py      # Release-readiness metadata audit
    description_quality.py # Shared description length/identity helpers
    llm.py                 # Unified LLM caller: Gemini / OpenAI / Anthropic from .env
    translate.py           # Public-service translation gap filler (MyMemory/LibreTranslate)
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
| Insecta | 47158 | Sounds only | 1 |
| Reptilia | 26036 | Sounds only | 1 |
| Amphibia | 20978 | Sounds only | 1 |

Mammals can define nested sub-taxa for focused coverage. The current v0.4
configuration includes `Chiroptera` (iNat taxon ID `40268`) under `Mammalia`
to include bat species with at least one sound observation, plus species with
at least 100 total iNaturalist observations.

## Taxonomy Rules

The biological taxonomy is species-level only. Scientific names must be clean
binomial species names: no genera-only rows, higher ranks, subspecies,
trinomials, hybrids, parenthetical qualifiers, slash alternatives, or informal
annotations. Subspecies encountered in source data are folded into their parent
binomial species for now.

Common names are also treated as display names. Parenthetical qualifiers,
bracketed notes, slash alternatives, and informal symbols are rejected in final
metadata. Regular spaces, apostrophes, and dashes/hyphens are allowed when they
are part of the accepted name.

### Birds

- AviList (currently v2026) is the authority for bird taxonomy and English bird
  names. Every iNaturalist bird resolves to its AviList species (by exact name,
  English name within the same genus or epithet stem, eBird code, or as an
  AviList subspecies) and the AviList scientific name becomes canonical.
- iNat taxa that AviList lumps or treats as subspecies (e.g. *Corvus cornix* →
  *Corvus corone*) are folded into that species as `scientific_name_aliases`.
- Reviewed exceptions in `overrides/bird_splits.csv` stay separate species
  (e.g. Hooded Crow *Corvus cornix*): they keep their own name, iNat data, eBird
  code, and English name (looked up by scientific name in the eBird taxonomy).
- Species AviList marks as extinct are excluded; possibly extinct and
  extinct-in-the-wild species are kept.

Non-bird groups are included through configured iNaturalist coverage rules and
reviewed priority species additions.

### iNaturalist taxa

Taxa that iNaturalist has retired (taxon swaps and merges) are dropped; their
names become aliases of the replacement taxon.

### Identifiers

- Every external identifier (eBird, GBIF, NCBI, Avibase, BirdLife, Macaulay
  Library, Xeno-Canto, observation.org, iNat, Wikidata) points to exactly one
  record. An ID reached only through an alias loses to the species whose own
  name carries it; IDs that stay ambiguous (typically after a recent split) are
  dropped. The build fails if a shared ID slips through.
- Identifiers must match their format: numeric GBIF, NCBI, BirdLife, and
  observation.org IDs; 16-hex Avibase IDs; lowercase eBird codes.

### Names

- When two species carry the same localized name, the more authoritative source
  (AviList > eBird > iNat > Wikidata) keeps it; on a tie, the species whose
  Wikipedia article in that language has the name as its title, else the
  more-observed one. The other loses that locale's name.
- Never used as a localized common name (the next source's name is used
  instead): a known scientific name, or (from iNat or Wikidata) an unrecorded
  synonym — a known genus with the species' epithet or a gender variant of it
  ("Leptodactylus andreae" for *Adenomera andreae*), unless the genus word
  heads longer vernacular names in that locale ("Colibri à gorge rubis"); a
  Latin-only name in a locale written in
  another script (an untranslated English fallback, e.g. in `zh`); a multi-word
  English name copied into another locale (eBird `pt_BR` lists untranslated
  species in lower-case English); file names, catalogue numbers, or names
  ending in a disambiguation number.
- When two non-bird species share an English display name on iNat, the one
  whose English Wikipedia lead gives that name keeps it (then one with no other
  English name, then the more-observed one). The others take an unused English
  name from their Wikipedia lead or iNat (spelling variants of the shared name
  don't count), else fall back to the scientific name.
- Non-bird English names get AviList-style capitalisation ("Heart-nosed Bat");
  bird names stay as AviList writes them. The English entry in `common_names`
  is always the display name, and absent when that is the scientific name.

### Descriptions

- Wikipedia descriptions must be about the species. An English article that
  names neither the species nor an alias (iNat sometimes links a sister species,
  the genus, or a clade) is dropped together with its other-language versions
  and any LLM text made from it, unless it is the article of a monotypic genus.
- Any description (Wikipedia in any language, or LLM) whose opening names
  another species of the taxonomy and never this one is dropped as well; the
  LLM translator treats such locales as missing so a translation takes their
  place. Links to rejected articles are dropped with them.
- An English article claimed by several species stays with the species it names
  first.
- Texts are cleaned of citation marks, sentences with leftover wiki markup,
  section headings, repeated sentences, and reference-list text (author–year
  citations, ISBNs, URLs, archive-link notes, link lists) that survived
  section selection.

### Images

A photo claimed by several species stays with one of them (the species named in
the file, else the most observed).

Configured non-species sound classes live under `sound_classes` in
`config.yml`. These records use their English display name as
`scientific_name`, carry `record_type: sound_class`, and are grouped separately
from biological taxa, for example `Anthropogenic` and `Geophony`.

## Pipeline

Run collectors in order — later steps depend on earlier output. All scripts are incremental (rerunning skips already-processed species). Use `--limit N` to cap new items per run, or `--dry-run` to preview.

| Step | Command | Output |
|------|---------|--------|
| 1. AviList | `python -m collectors.avilist` | `raw_data/AviList-*.csv` |
| 2. iNaturalist | `python -m collectors.inat` | `raw_data/inat_data.json` |
| 3. eBird | `python -m collectors.ebird` | `raw_data/ebird_data.json`, `raw_data/ebird_names.json`, `raw_data/ebird_taxonomy.json` |
| 4. Wikidata | `python -m collectors.wikidata` | `raw_data/wikidata_data.json` |
| 5. Wikipedia | `python -m collectors.wikipedia` | `raw_data/wikipedia_data.json` |
| 6. Macaulay Library | `python -m collectors.macaulay` | `raw_data/macaulay_data.json` |
| 7. Xeno-Canto | `python -m collectors.xenocanto` | `raw_data/xc_data.json` |
| 8. observation.org | `python -m collectors.observationorg` | `raw_data/observationorg_data.json` |
| 9. Sound Classes | `python -m collectors.sound_classes` | `raw_data/sound_classes.json` |
| 10. LLM Translation (optional) | `python -m collectors.translate` | `raw_data/translate_data.json` |
| 11. Images (optional) | `python -m collectors.images` | `dist/images/` (`--dev` → `dev/images/`) |
| 12. Build | `python -m build.metadata` | `dist/species_metadata.{json,csv,zip}` |

Steps 1–2 collect taxonomy. Steps 3–4 enrich species with eBird descriptions, common names, external identifiers, and Wikidata images. Step 5 fetches localized Wikipedia summaries. Steps 6–8 discover Macaulay Library taxon codes, Xeno-Canto name mappings, and observation.org species IDs for cross-referencing audio sources. Step 9 collects configured non-species sound classes and their Wikidata labels/Wikimedia images. Step 10 is optional LLM-based translation and description shortening, disabled by default. Step 11 downloads images. Step 12 merges everything into the final output — no API calls, purely offline.

### Step 1 — AviList

Downloads the AviList Global Avian Checklist (XLSX), converts to CSV. Provides authoritative bird taxonomy and AviList IDs. The checklist sheet is picked by its header, and v2026's upper-case headers (`SCIENTIFIC NAME`, `SPECIES CODE CORNELL LAB`, …) are normalized to the v2025 column names (`Scientific_name`, `Species_code_Cornell_Lab`, …) the pipeline reads. Set the release URL and CSV name under `avilist` in `config.yml`.

| Flag | Description |
|------|-------------|
| `--force` | Re-download even if CSV already exists |

### Step 2 — iNaturalist

Paginates the iNat taxa API to fetch all species for each taxon group. For birds, fetches all species. For other groups, queries the iNat sounds API to find species with audio observations meeting the `min_observations` threshold. Collects taxonomy, common names (all locales when `all_names: true`), observation counts, default photos, and Wikipedia URLs.

Reviewed issue-specific additions can be listed in `overrides/priority_species.csv`. These species are fetched after configured groups, preferably by iNaturalist taxon ID, and keep the normal species-only validation rules.

After group fetching, runs a **taxon status** phase: cached records are re-checked against the iNat taxa API (`is_active`, current name, `current_synonymous_taxon_ids`). The cache is incremental, so without this a taxon iNat retired in a swap or merge would linger next to its replacement as a duplicate species. Replacement taxa not yet cached are fetched. Records are checked once (`status_checked` date); `--refresh-status` re-checks all of them.

Then runs an **observation photo fallback** phase: for any species whose default taxon photo is missing or not CC-licensed, queries the iNat observations API for a CC-licensed photo from a research-grade observation (sorted by community votes). The result is stored in the `obs_photo` field, and unsuccessful lookups are cached in `inat_data.json` so later runs do not repeat the same slow checks.

| Flag | Description |
|------|-------------|
| `--group NAME` | Fetch only this taxon group |
| `--limit N` | Cap new species per group (0 = all) |
| `--new-only` | Only fetch new species; skip count/photo refresh work |
| `--save-every N` | Save progress every N new species (default: from config.yml) |
| `--refresh` | Bypass cached Phase 1 data and re-fetch from API |
| `--obs-photos-only` | Only run observation photo fallback |
| `--skip-obs-photos` | Skip observation photo fallback |
| `--refresh-obs-photos` | Recheck species cached as having no obs photo |
| `--avilist-only` | Only run AviList reconciliation |
| `--skip-avilist` | Skip AviList reconciliation phase |
| `--priority-only` | Only run reviewed priority species |
| `--skip-priority` | Skip reviewed priority species |
| `--status-only` | Only run the taxon status phase |
| `--skip-status` | Skip the taxon status phase |
| `--refresh-status` | Re-check the iNat status of all records, not just unchecked ones |
| `--refresh-names` | With the status phase, also refresh common names (all locales, plus every English alternative in `english_names`) for records fetched before alternatives were kept; reviewed names from `priority_species.csv` are re-applied |
| `--dry-run` | Preview without fetching |

### Step 3 — eBird

Collects eBird species data in two phases:

- **Phase 1 — Scraper:** Scrapes eBird species pages for English descriptions (`og:description`) and Macaulay Library images (`og:image`). Parallel fetching with configurable workers (default 4) and rate limiting (default 5 rps). Only for bird species with an eBird code; codes come from the built `raw_data/taxonomy.json` when present (validated against eBird), and each code is scraped once.
- **Phase 2 — Common names:** Downloads the eBird taxonomy CSV for 62 locales to collect common names in all available languages, and saves `raw_data/ebird_taxonomy.json` (eBird code → scientific name). Downloads are cached per eBird taxonomy version, so a new eBird release is fetched on the next run.

The build only assigns eBird codes that exist in `ebird_taxonomy.json`. AviList can carry codes eBird has not published yet, and it leaves the code empty where its species concept has no 1:1 eBird match; the build then falls back to eBird's own scientific name (e.g. `Tyto furcata` → `brnowl`) and finally Wikidata, never giving one code to two species.

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new species for scraping (0 = all) |
| `--workers N` | Parallel scrapers (default: 4) |
| `--rps N` | Max requests per second (default: 5) |
| `--names-only` | Only download common names (Phase 2) |
| `--skip-names` | Skip common names, scrape only |
| `--dry-run` | Preview without fetching |

### Step 4 — Wikidata

Fetches species identifiers, common name labels, and images from Wikidata and Wikimedia Commons via SPARQL queries.

- **Phase 1 — eBird codes:** Resolves eBird species codes (P3444) for species not yet matched via AviList, using scientific name (P225) and iNat taxon ID (P3151) as lookup keys.
- **Phase 2 — Identifiers:** Fetches external identifiers: GBIF (P846), NCBI (P685), Avibase (P2026), BirdLife (P5257). Identifier lookup tries reviewed aliases from `overrides/species_aliases.csv` and taxonomy-derived aliases. A species' own name wins over an alias, aliases shared by several species are skipped, and `id_sources` records which Wikidata taxon name supplied each ID so the build can reject IDs reached through an untrusted alias.
- **Phase 2c — Wikipedia sitelinks:** Records each species' Wikipedia article titles in the configured `wikipedia.locales` (`wikipedia_sitelinks`), so the Wikipedia collector can fetch articles iNat does not link.
- **Phase 2d — GBIF backbone match:** For species Wikidata has no GBIF ID for, queries the GBIF species match API (exact, species rank). An accepted match is the species' own ID; a synonym match records GBIF's accepted name in `id_sources`, so the build keeps it only when that name is a trusted alias. Misses are remembered (`gbif_match_checked`).
- **Phase 3 — Labels:** Fetches `rdfs:label` common names in all available languages.
- **Phase 4 — Images:** Fetches Wikidata P18 images and checks Wikimedia Commons licenses (CC, PD, GFDL).

| Flag | Description |
|------|-------------|
| `--new-only` | Only species not yet in wikidata_data.json |
| `--ids-only` | Only query external identifiers; skip labels and images |
| `--refresh-identifiers` | Replace existing GBIF/NCBI/Avibase/BirdLife IDs with fresh Wikidata values |
| `--limit N` | Cap species queried in this run (0 = all) |
| `--no-cache` | Bypass request cache |
| `--dry-run` | Show species count without querying |

### Step 5 — Wikipedia

Fetches multilingual Wikipedia data in four phases:

- **Phase 1 — English Wikipedia:** Batch-fetches extracts, langlinks, page images, and Wikidata descriptions for each species' Wikipedia article — the article iNat links, or for species iNat links none, the English article from Wikidata sitelinks. Up to 50 titles per request. Includes a search fallback for titles not found in batch results.
- **Non-English-only articles:** Species without any English article but with articles in other configured locales (Wikidata sitelinks) get those intro extracts directly (identity-checked against the scientific name; disambiguation pages skipped).
- **Phase 1b — Extract backfill:** Scans existing data for species that have an English Wikipedia URL but are missing the English extract (can happen due to API glitches during bulk fetching). Re-fetches just the extracts for those species with redirect resolution.
- **Phase 2 — Locale extracts:** For each target language (20 configured locales), batch-fetches intro extracts from the corresponding Wikipedia. Runs locales concurrently with a thread pool. Skips species that already have extracts for a given locale.
- **Phase 3 — Image licenses:** Batch-fetches license metadata (artist, license, license URL) from Wikimedia Commons for all page images found in Phase 1.
- **Quality refetch — description depth:** Optional `--quality-refetch` pass for missing or too-short extracts in all configured Wikipedia locales. It fetches richer plain-text article content from the matching language wiki (one title per request: whole-page extracts are limited to one page per API call), validates that the article title/text contains the canonical scientific name or a clean alias, stores per-locale retry/status metadata, and avoids retrying indefinitely. The text is the lead plus early sections about the animal (taxonomy/etymology sections are skipped, references end the selection, headings are never kept), capped at `descriptions.max_words`. Disambiguation pages ("Ruff may refer to:") are retried under the scientific name; the build drops any disambiguation text and stray section headings from stored descriptions. The selector can keep multiple early paragraphs through `descriptions.wikipedia_min_paragraphs`. Use `--quality-locales en` to restrict this to the English release gate.

Rate-limited (default 25 rps), with exponential backoff on 429s and server errors. All phases save incrementally.
Description quality thresholds such as minimum English word count, target
length, extra early sections, and minimum retained paragraphs are configured
under `descriptions` in `config.yml`.

Missing locale excerpts can be filled from English Wikipedia excerpts with an
optional public translation-service utility:

```bash
python -m utils.translate --dry-run
python -m utils.translate --locales de,es,fr,cs,it,pt --limit 100
```

The utility is incremental and only fills blank locale excerpts when English
Wikipedia text exists. It stores per-locale provenance in `extract_sources`,
for example `Source: Wikipedia, translated by LibreTranslate`, which the build
carries through to `description_sources`. Service endpoint, rate limit, and
default target locales live under `translation` in `config.yml`. By default it
only fills German, Spanish, French, Czech (`cs`), Italian, and Portuguese. If
the selected service needs a key, set the configured `LIBRETRANSLATE_API_KEY`
environment variable.

**Wikipedia locales:** en, de, fr, es, pt, it, nl, pl, sv, da, no, fi, cs, zh, ru, ar, ja, ko, tr, sw

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new species (0 = all) |
| `--rps N` | Max requests per second (default: 25) |
| `--new-only` | Only species not yet in wikipedia_data.json |
| `--refetch` | Re-fetch species with few locale extracts (conflicts with `--new-only`) |
| `--quality-refetch` | Re-fetch missing/short extracts using richer article text |
| `--quality-locales LIST` | `all` configured locales, or comma-separated locales such as `en,de,fr` |
| `--quality-max-attempts N` | Skip quality refetch after N attempts per species |
| `--dry-run` | Preview without fetching |

### Step 6 — Macaulay Library

Discovers Macaulay Library taxon codes for all species. Birds use their eBird species code (e.g. `eurblk1`); non-birds get a `t-`prefixed numeric ID (e.g. `t-11032766`) resolved via the ML taxonomy API.

Resolution cascade:
1. **eBird code** — reuses existing eBird species code for birds (instant, no API call). The build always sets a bird's ML code to its validated eBird code, since cached ML codes can predate a split
2. **ML taxonomy API** — queries by scientific name for non-birds
3. **Wikidata P10794** — bulk SPARQL lookup of Macaulay Library taxon IDs
4. **Alias synonym fallback** — tries reviewed aliases from `overrides/species_aliases.csv`, then GBIF synonyms, and retries the ML API

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new species to process (0 = all) |
| `--group NAME` | Process only this taxon group |
| `--new-only` | Only species not yet in macaulay_data.json |
| `--recheck-shared` | Re-resolve species whose cached ML code is shared with another species |
| `--dry-run` | Preview without API calls |

### Step 7 — Xeno-Canto

Maps each species to its Xeno-Canto scientific name. XC uses IOC taxonomy which may differ from the iNat/eBird names used in this pipeline (e.g. `Dryobates pubescens` → `Picoides pubescens`). Requires an API key in `.env` (`XC_API_KEY=...`).

Resolution cascade:
1. **Wikidata P2426** — bulk SPARQL fetch of XC species IDs (~31k species pre-mapped)
2. **XC API direct** — queries by genus + epithet
3. **XC API epithet search** — epithet-only search within the species' XC group (catches genus transfers), accepted only when XC reports a single species with that epithet in the group; birds, mammals, and amphibians only (insect epithets collide across orders XC doesn't cover, and XC has no reptile group)
4. **Alias synonym fallback** — tries reviewed aliases from `overrides/species_aliases.csv`, then GBIF synonyms, and retries the XC API
5. **XC English name search** — last resort, matches by common name within the species' XC groups

Each entry records how it was matched (`method`). Failed requests are never cached as "no XC name" (the run stops after 20 consecutive failures, e.g. during an XC outage; rerun later to continue). Fuzzy matches (3, 5) that are another species' name or alias are rejected. The build validates cached names again: an XC name must be the species' own name or alias, or a genus transfer within the same taxon group that is not another species' name.

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new species to process (0 = all) |
| `--group NAME` | Process only this taxon group |
| `--new-only` | Only species not yet in xc_data.json |
| `--retry-unresolved` | Retry species cached with no XC mapping |
| `--revalidate` | Drop cached XC names that belong to another species (or fuzzy matches for reptiles) and re-resolve them |
| `--dry-run` | Preview without API calls |

### Step 8 — observation.org

Maps each species to its [observation.org](https://observation.org) species ID, enabling direct links to species pages on the platform. Observation.org uses AviList taxonomy for birds (same authority as this pipeline), so matching is straightforward.

Resolution cascade:
1. **Direct API search** — queries `/api/v1/species/search/` by scientific name
2. **Alias synonym fallback** — tries reviewed aliases from `overrides/species_aliases.csv`, then GBIF synonyms, and retries the API

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new species to process (0 = all) |
| `--group NAME` | Process only this taxon group |
| `--workers N` | Parallel workers (default: from config.yml) |
| `--save-every N` | Save every N completed species (default: from config.yml) |
| `--new-only` | Only species not yet in observationorg_data.json |
| `--retry-unresolved` | Retry species cached with no observation.org ID |
| `--recheck-shared` | Re-resolve species whose cached ID is shared with another species (e.g. after a split) |
| `--dry-run` | Preview without API calls |

### Step 9 — Sound Classes

Collects configured non-species sound classes from `config.yml`. Each entry uses
its English common name as the final `scientific_name`, fetches localized labels
from Wikidata, and uses a licensed Wikimedia Commons image when available.
Set `image_file` to a Commons filename to override the Wikidata P18 image for a
specific class. Use `overrides/species_overrides.csv` for fixed crop anchors.
Current classes include anthropogenic sounds such as power tools, siren, gun,
chainsaw, engine, and human, plus geophony classes such as rain, thunder, and
wind. `Human` includes common-name aliases for `human vocal` and
`human non-vocal` so those searches resolve to the canonical `Human` entry.

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new sound classes to process (0 = all) |
| `--new-only` | Only process sound classes not already cached |
| `--dry-run` | Preview without fetching |

### Step 10 — LLM Translation

Uses an LLM (Gemini, OpenAI, or Anthropic) for three tasks on existing Wikipedia extracts — no content is generated from scratch:

- **Phase 1 — Shorten:** Finds extracts exceeding `max_extract_words` (default 500 words) and asks the LLM to condense them to `target_words` (default 150 words), preserving the original language and key facts (appearance, habitat, range, behaviour).
- **Phase 2 — Translate:** Finds species that have an English extract but are missing translations for the configured target locales — a native Wikipedia stub under 25 words counts as missing when the English text has at least 40 words. Sends the English text for translation into all missing locales at once.
- **Phase 3 — English fallback:** Optional `--fallback-missing` pass that creates English descriptions only from traceable Wikipedia extracts: for species without English text, from their non-English extracts; for English stubs (under `descriptions.min_english_words`) whose non-English article is at least twice as long, from the stub plus those extracts. Fallback locales are marked as `llm_fallback` in build output.

Translations record a hash of their English source, so `--retranslate-stale` can redo them when the English text changes; the build prefers native Wikipedia text added later over an older machine translation for the same locale. When the provider keeps answering HTTP 429 after retries (quota exhausted), the run stops sending requests and saves; rerun later to continue.

LLM output is stored in `translate_data.json` and overlaid on Wikipedia extracts during the build step. The LLM only fills gaps — it never overwrites existing Wikipedia extracts for a locale.

**Provider selection:** the provider is auto-detected from whichever key is present in `.env`, with priority `GEMINI_API_KEY` → `OPENAI_API_KEY` → `ANTHROPIC_API_KEY`. Add the relevant key(s) to `.env`:

```
GEMINI_API_KEY=...        # gemini-2.0-flash (default for Gemini)
OPENAI_API_KEY=...        # gpt-4o-mini (default for OpenAI)
ANTHROPIC_API_KEY=...     # claude-haiku-4-5-20251001 (default for Anthropic)
```

Override the provider or model per run:

```bash
python -m collectors.translate --provider gemini
python -m collectors.translate --provider openai --model gpt-4o
```

**Target locales:** en, de, fr, es, pt, it, nl, zh, ru, ar (subset of Wikipedia locales, configurable under `llm.locales` in `config.yml`)

This step is disabled by default (`llm.enabled: false`). Enable it in `config.yml` to include LLM descriptions in the build output.

| Flag | Description |
|------|-------------|
| `--limit N` | Cap total work items (0 = all) |
| `--provider NAME` | `gemini`, `openai`, or `anthropic` (auto-detected from `.env`) |
| `--model NAME` | Override the provider default model |
| `--batch-size N` | Species per API call (default: 12) |
| `--workers N` | Parallel translation workers (default: 4) |
| `--char-budget N` | Source-character budget per API call (default: 12000) |
| `--max-source-chars N` | Max source chars per species sent to LLM |
| `--save-every N` | Save every N completed batches |
| `--shorten-only` | Only shorten long extracts |
| `--translate-only` | Only translate missing locales |
| `--fallback-missing` | Generate English fallbacks from non-English Wikipedia source extracts |
| `--skip-stubs` | Only translate locales without any text; leave native Wikipedia stubs in place |
| `--retranslate-stale` | Also redo LLM translations made from an older English source (translations record a hash of their source; older ones count as stale when the English text is now at least twice as long) |
| `--fallback-only` | Only generate English fallbacks |
| `--dry-run` | Preview without API calls |

### Step 11 — Images

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

| Flag | Description |
|------|-------------|
| `--limit N` | Cap species to process (0 = all) |
| `--workers N` | Parallel download threads (default: from config.yml) |
| `--quality N` | WebP quality 1–100 (default: from config.yml) |
| `--dev` | Save to dev/images/ instead of dist/images/ |
| `--new-only` | Only species with no cached image files yet |
| `--dry-run` | Preview without downloading |

### Step 12 — Build

Merges all pre-collected data into the final metadata file. Runs purely offline — no API calls. Two phases:

**Taxonomy phase:**
1. Cross-references iNaturalist, AviList, Wikidata, and configured sound classes to build a canonical entry list (see [Taxonomy Rules](#taxonomy-rules): retired iNat taxa are dropped, birds take their AviList name, lumped/subspecies taxa are folded into aliases)
2. Resolves eBird codes from AviList, eBird's own scientific names, and Wikidata, validated against `raw_data/ebird_taxonomy.json` and unique per species
3. Loads external identifiers from Wikidata (GBIF, NCBI, Avibase, BirdLife), Macaulay Library, Xeno-Canto (validated against other species' names), and observation.org; an identifier claimed by several species stays only with the species whose own name carries it
4. Collects common names from iNaturalist, eBird (62 locales), and Wikidata labels; names shared between species in a locale stay with the most authoritative source
5. Raw data collected under an older name (e.g. Wikipedia text keyed by the iNat name of a bird AviList renamed) is found through the species' source names
6. Selects the best image for each species through a priority chain:
   - **iNaturalist taxon photo** — default photo if CC-licensed
   - **Macaulay Library** — eBird image (source tagged as "Macaulay Library ML{asset_id}")
   - **Wikimedia Commons** — Wikidata P18 image if CC/PD/GFDL licensed
   - **iNaturalist observation photo** — CC-licensed photo from research-grade observations (last resort)

   eBird placeholder images (no asset ID) and generic eBird page text are ignored, Wikimedia SVG files (drawings, maps) are skipped, and Wikimedia license names are written in the iNat style (`CC BY-SA 4.0` → `cc-by-sa-4.0`, `Public domain` → `pd`).

**Merge phase:**
Assembles per-species descriptions from multiple sources with the following priority:
- **Base layer — Wikipedia:** English extract plus all locale extracts and Wikipedia URLs
- **Fallback — eBird:** English description only, used when Wikipedia has no English extract
- **LLM overlay:** Disabled by default. When `llm.enabled: true`, each locale the LLM provides replaces the description for that locale. LLM locales are tracked in the `translate_locales` field

The effective priority is **LLM > Wikipedia > eBird**, applied per-locale,
when LLM translation is enabled. Default builds use only Wikipedia and eBird
descriptions.
The JSON output also includes `description_sources`, a per-locale source map,
and `scientific_name_aliases`, a list of clean binomial scientific names that
resolve to the canonical species. Non-species sound classes use
`record_type: sound_class`; their `scientific_name_aliases` and
`common_name_aliases` are both search fallbacks rather than biological names.
Reviewed manual scientific-name bridges live in `overrides/species_aliases.csv`.
The build also filters source-derived aliases that collide with another
canonical species and fails if any alias conflict remains.

The build also computes `metadata_quality_score` and
`metadata_quality_flags` before BirdNET IDs are assigned. When
`metadata_quality.enabled` is true, species below `metadata_quality.min_score`
or matching the configured thin-record rule are dropped and listed in the
configured report CSV, defaulting to `dev/metadata_quality_report.csv`.

The JSON output contains full multilingual descriptions and every collected common-name locale. The CSV output is a lighter export and does not include description excerpts; scientific aliases and metadata quality flags are exported as pipe-separated fields. It has one `common_name_<locale>` column per locale listed under `csv_export.locales` in `config.yml`, so the column set stays stable between builds.

Image fields in the final metadata:

- JSON metadata stores an `image` object with `src`, `thumb`, and `medium`
- CSV metadata flattens this to a single `image_url` column containing the local served medium image URL

**BirdNET species IDs:**
Each species receives a BirdNET ID in the format `BN{5 digits}` (e.g. `BN00726`), stored in the git-tracked `bn_ids.json` registry. Before the 1.0 release, IDs may be intentionally reassigned with `--reassign-ids` as a documented breaking change. Normal builds preserve existing IDs and assign new IDs only to newly added species.

Downstream consumers should not use `birdnet_id` as a join key across pre-1.0 taxonomy versions; match on `scientific_name`, `scientific_name_aliases`, or an external identifier instead. IDs were last reassigned for the AviList v2026 / taxonomy-cleanup build (retired iNat taxa removed, AviList lumps folded, extinct birds excluded).

| Flag | Description |
|------|-------------|
| `--dev` | Write to dev/ instead of dist/ |
| `--merge-only` | Skip taxonomy rebuild, re-merge only |
| `--no-zip` | Skip zip archive creation |
| `--reassign-ids` | Regenerate all BirdNET IDs from scratch (pre-release only) |
| `--strict-validation` | Fail if release-gate metadata audit findings remain |
| `--dry-run` | Show stats without writing |

Focused description coverage can be audited with:

```bash
python -m utils.audit_descriptions dist/species_metadata.json --output dev/description_audit.csv
```

By default this report includes short excerpts in every available locale plus
English release-gate gaps. Add `--english-only` to report only the English gate.
Prefer Wikipedia quality refetches before optional LLM fallback:

```bash
python -m collectors.wikipedia --quality-refetch
```

LLM translation is disabled by default and should only be enabled deliberately
for a release that accepts generated description text.

The `raw_data/`, `dev/`, and `dist/` directories are all gitignored. Zip archives from `dist/` are attached to GitHub releases.

### Web Server

Browse and search the dataset through a web UI and REST API.

| Flag | Description |
|------|-------------|
| `--host ADDR` | Bind address (default: 127.0.0.1) |
| `--port N` | Bind port (default: 8000) |
| `--dev` | Load metadata from dev/ instead of dist/ |
| `--reload` | Auto-reload on code changes |

Or with hot-reload during development:

```bash
uvicorn web.server:app --reload
```

**Species lookup** supports multiple identifier types. The `/species/{name}` and `/api/species/{name}` endpoints accept any of:
- Scientific name (e.g., `Turdus merula`)
- Common name in any locale (e.g., `Amsel`, `Merle noir`)
- BirdNET ID (e.g., `BN10600`)
- Scientific alias from `scientific_name_aliases`
- Common-name alias from `common_name_aliases`
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
| `description_source` | `?description_source=llm,wikipedia` | Filter by description source |
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
- **[Wikipedia](https://www.wikipedia.org)** — English summaries, localized article links, and optional public-service translations from English Wikipedia excerpts. Content available under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- **[AviList](https://www.avilist.org)** — The Global Avian Checklist (v2026). AviList Core Team, 2026. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). doi:[10.2173/avilist.v2026](https://doi.org/10.2173/avilist.v2026).
- **[Macaulay Library](https://www.macaulaylibrary.org)** — Taxon codes for cross-referencing audio and visual media from the Cornell Lab of Ornithology.
- **[Xeno-Canto](https://xeno-canto.org)** — Scientific name mappings for cross-referencing the world's largest shared bird and wildlife sound collection.
- **[observation.org](https://observation.org)** — Species IDs for cross-referencing to one of Europe's largest biodiversity recording platforms. Uses AviList taxonomy for birds.
- **LLM providers** — Optional AI-powered translation of Wikipedia extracts to missing locales and shortening of excessively long extracts. Supported: [Google Gemini](https://ai.google.dev/) (`GEMINI_API_KEY`), [OpenAI](https://openai.com/) (`OPENAI_API_KEY`), [Anthropic Claude](https://www.anthropic.com/claude) (`ANTHROPIC_API_KEY`). Provider is auto-detected from `.env`.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Funding

Our work in the Cornell K. Lisa Yang Center for Conservation Bioacoustics is made possible by the generosity of K. Lisa Yang to advance innovative conservation technologies to inspire and inform the conservation of wildlife and habitats.

The development of BirdNET is supported by the German Federal Ministry of Research, Technology and Space (FKZ 01|S22072), the German Federal Ministry for the Environment, Climate Action, Nature Conservation and Nuclear Safety (FKZ 67KI31040E), the German Federal Ministry of Economic Affairs and Energy (FKZ 16KN095550), the Deutsche Bundesstiftung Umwelt (project 39263/01) and the European Social Fund.

## Partners

BirdNET is a joint effort of partners from academia and industry.
Without these partnerships, this project would not have been possible.
Thank you!

![Logos of all partners](https://tuc.cloud/index.php/s/KSdWfX5CnSRpRgQ/download/box_logos.png)
