# Pipeline

How each collector works, what it stores, and which flags it takes. For the fields in the final metadata, see [metadata.md](metadata.md).

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
| 9. Avicommons | `python -m collectors.avicommons` | `raw_data/avicommons_data.json` |
| 10. Sound Classes | `python -m collectors.sound_classes` | `raw_data/sound_classes.json` |
| 11. LLM Translation (optional) | `python -m collectors.translate` | `raw_data/translate_data.json` |
| 12. Build | `python -m build.metadata` | `dist/species_metadata.{json,csv,zip}`, `dist/model_labels/`, `raw_data/taxonomy.json` |
| 13. Images (optional) | `python -m collectors.images` | `dist/images/` (`--dev` → `dev/images/`) |

Steps 1–2 collect taxonomy. Steps 3–4 enrich species with common names, external identifiers, and Wikidata images. Step 5 fetches localized Wikipedia summaries. Steps 6–8 discover Macaulay Library taxon codes, Xeno-Canto name mappings, and observation.org species IDs for cross-referencing audio sources. Step 9 downloads the Avicommons bird photo index, referenced next to each bird's own image. Step 10 collects configured non-species sound classes and their Wikidata labels/Wikimedia images. Step 11 is optional LLM-based translation and description shortening (enabled through `llm.enabled` in `config.yml`). Step 12 merges everything into the final output — no API calls, purely offline — and also writes `raw_data/taxonomy.json`, which later collector runs use as their species list. Step 13 downloads and crops images for the built species list.

## Step 1 — AviList

Downloads the AviList Global Avian Checklist (XLSX), converts to CSV. Provides authoritative bird taxonomy and AviList IDs. The checklist sheet is picked by its header, and v2026's upper-case headers (`SCIENTIFIC NAME`, `SPECIES CODE CORNELL LAB`, …) are normalized to the v2025 column names (`Scientific_name`, `Species_code_Cornell_Lab`, …) the pipeline reads. Set the release URL and CSV name under `avilist` in `config.yml`.

| Flag | Description |
|------|-------------|
| `--force` | Re-download even if CSV already exists |

## Step 2 — iNaturalist

Paginates the iNat taxa API to fetch all species for each taxon group. For birds, fetches all species. For other groups, queries the iNat sounds API to find species with audio observations meeting the `min_observations` threshold. Collects taxonomy, common names (all locales when `all_names: true`), observation counts, default photos, and Wikipedia URLs.

Reviewed issue-specific additions can be listed in `overrides/priority_species.csv`. These species are fetched after configured groups, preferably by iNaturalist taxon ID, and keep the normal species-only validation rules. A species iNaturalist does not list stays in as a record without an iNat taxon.

After group fetching, runs a **taxon status** phase: cached records are re-checked against the iNat taxa API (`is_active`, current name, `current_synonymous_taxon_ids`). The cache is incremental, so without this a taxon iNat retired in a swap or merge would linger next to its replacement as a duplicate species. Replacement taxa not yet cached are fetched. Records are checked once (`status_checked` date); `--refresh-status` re-checks all of them and also refreshes default photos (iNat photos get replaced, deleted, or relicensed) and observation counts.

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

## Step 3 — eBird

Collects eBird species data in two phases:

- **Phase 1 — Scraper:** Scrapes eBird species pages for Macaulay Library images (`og:image`). It also stores the English description (`og:description`), which is never published. Parallel fetching with configurable workers (default 4) and rate limiting (default 5 rps). Only for bird species with an eBird code; codes come from the built `raw_data/taxonomy.json` when present (validated against eBird), and each code is scraped once.
- **Phase 2 — Common names:** Downloads the eBird taxonomy CSV for 63 locales to collect common names in all available languages, and saves `raw_data/ebird_taxonomy.json` (eBird code → scientific name). Downloads are cached per eBird taxonomy version, so a new eBird release is fetched on the next run.

The build only assigns eBird codes that exist in `ebird_taxonomy.json`. AviList can carry codes eBird has not published yet, and it leaves the code empty where its species concept has no 1:1 eBird match; the build then falls back to eBird's own scientific name (e.g. `Tyto furcata` → `brnowl`) and finally Wikidata, never giving one code to two species.

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new species for scraping (0 = all) |
| `--workers N` | Parallel scrapers (default: 4) |
| `--rps N` | Max requests per second (default: 5) |
| `--names-only` | Only download common names (Phase 2) |
| `--skip-names` | Skip common names, scrape only |
| `--dry-run` | Preview without fetching |

## Step 4 — Wikidata

Fetches species identifiers, common name labels, and images from Wikidata and Wikimedia Commons via SPARQL queries.

- **Phase 1 — eBird codes:** Resolves eBird species codes (P3444) for species not yet matched via AviList, using scientific name (P225) and iNat taxon ID (P3151) as lookup keys.
- **Phase 2 — Identifiers:** Fetches external identifiers: GBIF (P846), NCBI (P685), Avibase (P2026), BirdLife (P5257), and the matched Wikidata item (`wikidata_qid`: the item behind the GBIF ID, else the NCBI ID, else the first match; lexeme senses are ignored, duplicate items resolve to the one with more identifiers). Stored IDs are kept; only gaps are filled. Identifier lookup tries reviewed aliases from `overrides/species_aliases.csv` and taxonomy-derived aliases. A species' own name wins over an alias, aliases shared by several species are skipped, and `id_sources` records which Wikidata taxon name supplied each ID so the build can reject IDs reached through an untrusted alias.
- **Phase 2c — Wikipedia sitelinks:** Records each species' Wikipedia article titles in the configured `wikipedia.locales` (`wikipedia_sitelinks`), so the Wikipedia collector can fetch articles iNat does not link.
- **Phase 2d — GBIF backbone match:** For species Wikidata has no GBIF ID for, queries the GBIF species match API (exact, species rank). An accepted match is the species' own ID; a synonym match records GBIF's accepted name in `id_sources`, so the build keeps it only when that name is a trusted alias. Misses are remembered (`gbif_match_checked`).
- **Phase 2e — GBIF key status:** Looks up each stored GBIF key in the GBIF backbone (`gbif_status`: status, accepted key and name). Wikidata often holds the key of a GBIF synonym usage, e.g. GBIF still lists *Curruca nisoria* as *Sylvia nisoria*; such keys return few occurrences.
- **Phase 3 — Labels:** Fetches `rdfs:label` common names in all available languages.
- **Phase 4 — Images:** Fetches Wikidata P18 images and checks Wikimedia Commons licenses (CC, PD, GFDL).

| Flag | Description |
|------|-------------|
| `--new-only` | Only species not yet in wikidata_data.json |
| `--ids-only` | Only query external identifiers; skip labels and images |
| `--refresh-identifiers` | Replace existing GBIF/NCBI/Avibase/BirdLife IDs and Wikidata QIDs with fresh Wikidata values |
| `--limit N` | Cap species queried in this run (0 = all) |
| `--no-cache` | Bypass request cache |
| `--dry-run` | Show species count without querying |

## Step 5 — Wikipedia

Fetches multilingual Wikipedia data in these phases:

- **Phase 1 — English Wikipedia:** Batch-fetches extracts, langlinks, page images, and Wikidata descriptions for each species' Wikipedia article — the article iNat links, or for species iNat links none, the English article from Wikidata sitelinks. Up to 50 titles per request. Includes a search fallback for titles not found in batch results.
- **Article lookup by name:** Species without a usable English article — none linked, or the linked one fails the build's identity check (iNat often links a split's parent species or the genus, e.g. "Vermilion flycatcher" for *Pyrocephalus rubinus*) — are looked up under their Wikidata English sitelink, scientific name, older names with the same epithet (genus transfers, e.g. *Litoria caerulea* for *Pelodryas caerulea*), and English name. A page is accepted only if its title or the binomial its intro opens with is the species or the same epithet in another genus that is not another species here (a split's name often redirects to the parent's article, which opens with the parent's name; an alias with another epithet is not enough), it passes the build's identity checks, and no other species already uses it. A replaced article's record is refetched, and LLM texts made from the old article are dropped from `translate_data.json` so `collectors.translate` redoes them. The stored article then stays the species' article on later runs.
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
for example `Source: Wikipedia, translated by MyMemory`, which the build
carries through to `description_sources`. Service endpoint, rate limit, and
default target locales live under `translation` in `config.yml`. By default it
uses MyMemory (`translation.email` / `--email` doubles the free daily quota)
and only fills German, Spanish, French, Czech (`cs`), Italian, and Portuguese.
For LibreTranslate, use `--provider libretranslate` and, if the instance needs
a key, name its variable in `translation.api_key_env` (e.g.
`LIBRETRANSLATE_API_KEY`) and set it in `.env`.

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

## Step 6 — Macaulay Library

Discovers Macaulay Library taxon codes for all species. Birds use their eBird species code (e.g. `eurbla`); non-birds get a `t-`prefixed numeric ID (e.g. `t-11032766`) resolved via the ML taxonomy API.

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

## Step 7 — Xeno-Canto

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

## Step 8 — observation.org

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

## Step 9 — Avicommons

Downloads the full JSON index of [Avicommons](https://avicommons.org), a hand-picked set of cropped, Creative-Commons-licensed bird photos keyed by eBird species code (eBird/Clements taxonomy). Avicommons asks consumers to fetch the file once rather than on every page load, so it is cached in `raw_data/avicommons_data.json` and the build reads the local copy. Set the release file (e.g. `https://avicommons.org/2025.json`), image host, and referenced pixel widths under `avicommons` in `config.yml`; a changed URL is downloaded on the next run.

The build never uses an Avicommons photo as the species image: our own image selection is unchanged, and the matching Avicommons photo is referenced alongside it in `avicommons_image` (see [Image fields](metadata.md#image-fields)). Matching:

- by the bird's validated `ebird_code`, so each Avicommons photo goes to at most one species;
- only if Avicommons names the same species: its scientific name is the species' own name or one of its aliases (Avicommons follows eBird, which can lag an AviList genus move, e.g. *Emberiza schoeniclus* → *Schoeniclus schoeniclus*), or its English name equals the species' `common_name` or `common_name_alt`. Mismatches are reported by the build and dropped;
- species AviList lumps get no reference from the lumped taxa's photos, since those codes are not the species' own;
- a photo Avicommons lists under several species codes is dropped, as are photos whose license needs attribution but have no photographer.

Licenses are written in the iNat style used elsewhere (`CC BY-NC 2.0` → `cc-by-nc-2.0`; Avicommons' `CC0 2.0` etc. → `cc0`). Most Avicommons photos are non-commercial (`cc-by-nc*`); check `avicommons_image.license` before reuse.

| Flag | Description |
|------|-------------|
| `--force` | Re-download even if the configured release is cached |
| `--dry-run` | Show what would be downloaded without fetching |

## Step 10 — Sound Classes

Collects the non-species sound classes in `overrides/sound_classes.csv`. Each row
(`name`, `group`, `wikidata_qid`, `audioset_id`, `parent`, `image_file`, `aliases`,
`scientific_aliases`, `description`) uses its English common name as the final
`scientific_name`. The collector fetches localized labels from Wikidata and uses a
licensed Wikimedia Commons image when available. SVG files are skipped.
Set `image_file` to a Commons filename to override the Wikidata P18 image for a
specific class. Use `overrides/species_overrides.csv` for fixed crop anchors.
Classes cover machines and tools, music, geophony, human sounds, and a few top-level
soundscape categories. `audioset_id` and `parent` place a class in the AudioSet ontology
(`parent` is the nearest broader class we hold), and `description` holds AudioSet's own
wording, published with `description_source: audioset`.
Each class can list `|`-separated common-name aliases (e.g. `chain saw` and `power saw`
for `Chainsaw`) so those searches resolve to the canonical entry. Human sounds are
individual classes in the `Human` group, and redirect to the species record *Homo
sapiens* (Mammalia) in the model crosswalk. *Homo sapiens* keeps search aliases such as
`human vocal` and `speech` from `overrides/common_name_aliases.csv`; a class's own name
still wins an exact lookup. The build includes only classes listed in the CSV; a class
removed there is dropped from the build even though the collector's cache keeps it.

| Flag | Description |
|------|-------------|
| `--limit N` | Cap new sound classes to process (0 = all) |
| `--new-only` | Only process sound classes not already cached |
| `--dry-run` | Preview without fetching |

## Step 11 — LLM Translation

Uses an LLM (Gemini, OpenAI, or Anthropic) for three tasks on existing Wikipedia extracts — no content is generated from scratch:

- **Phase 1 — Shorten:** Finds extracts exceeding `max_extract_words` (default 500 words) and asks the LLM to condense them to `target_words` (default 150 words), preserving the original language and key facts (appearance, habitat, range, behaviour).
- **Phase 2 — Translate:** Finds species that have an English extract but are missing translations for the configured target locales — a native Wikipedia stub under 25 words counts as missing when the English text has at least 40 words. Sends the English text for translation into all missing locales at once.
- **Phase 3 — English fallback:** Optional `--fallback-missing` pass that creates English descriptions only from traceable Wikipedia extracts: for species without English text, from their non-English extracts; for English stubs (under `descriptions.min_english_words`) whose non-English article is at least twice as long, from the stub plus those extracts. Fallback locales are marked as `llm_fallback` in build output.

All phases work on Wikipedia text cleaned the way the build cleans it (no
reference lists, subspecies lists, or markup), so stub checks and sources match
the release. Prompts list each species' localized common names from
`taxonomy.json`; the model must use them, or the scientific name where a
language has none, instead of inventing a vernacular name. Translations record a hash of their English source, so `--retranslate-stale` can redo them when the English text changes; the build prefers native Wikipedia text added later over an older machine translation for the same locale. When the provider keeps answering HTTP 429 after retries (quota exhausted), the run stops sending requests and saves; rerun later to continue.

LLM output is stored in `translate_data.json` and overlaid on Wikipedia extracts during the build step: shortened extracts replace the long originals, and translations fill missing locales or replace native stubs. A translation yields to native Wikipedia text of at least max(25, half the translation's) words, e.g. an article added after the translation was made. An English fallback yields to native English text of at least `descriptions.min_english_words`, e.g. an article found by name later.

**Provider selection:** the provider is auto-detected from whichever key is present in `.env`, with priority `GEMINI_API_KEY` → `OPENAI_API_KEY` → `ANTHROPIC_API_KEY`. Add the relevant key(s) to `.env`:

```
GEMINI_API_KEY=...        # gemini-3.1-flash-lite (default for Gemini)
OPENAI_API_KEY=...        # gpt-4o-mini (default for OpenAI)
ANTHROPIC_API_KEY=...     # claude-haiku-4-5-20251001 (default for Anthropic)
```

Override the provider or model per run:

```bash
python -m collectors.translate --provider gemini
python -m collectors.translate --provider openai --model gpt-4o
```

**Target locales:** de, fr, it, es, cs, pt, ru, pl, nl (configurable under `llm.locales` in `config.yml`; English fallbacks are written separately).

LLM descriptions are merged into the build while `llm.enabled` is true in `config.yml` (as in the current configuration and the v0.5 release); set it to `false` to build from Wikipedia texts only.

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
| `--species-file PATH` | Only process the species in this file, one name per line |
| `--dry-run` | Preview without API calls |

## Step 12 — Build

Merges all pre-collected data into the final metadata file. Runs purely offline — no API calls. Two phases:

**Taxonomy phase:**
1. Cross-references iNaturalist, AviList, Wikidata, and the reviewed sound classes to build a canonical entry list (see [Taxonomy Rules](taxonomy.md): retired iNat taxa are dropped, birds take their AviList name, lumped/subspecies taxa are folded into aliases)
2. Resolves eBird codes from AviList, eBird's own scientific names, and Wikidata, validated against `raw_data/ebird_taxonomy.json` and unique per species
3. Loads external identifiers from Wikidata (GBIF, NCBI, Avibase, BirdLife, Wikidata QID), Macaulay Library, Xeno-Canto (validated against other species' names), and observation.org; an identifier claimed by several species stays only with the species whose own name carries it
4. Collects common names from iNaturalist, eBird (63 locales), and Wikidata labels; names shared between species in a locale stay with the most authoritative source
5. Raw data collected under an older name (e.g. Wikipedia text keyed by the iNat name of a bird AviList renamed) is found through the species' source names
6. Selects the best image for each species through a priority chain:
   - **iNaturalist taxon photo** — default photo if CC-licensed
   - **Macaulay Library** — eBird image (source tagged as "Macaulay Library ML{asset_id}")
   - **Wikimedia Commons** — Wikidata P18 image if CC/PD/GFDL licensed
   - **iNaturalist observation photo** — CC-licensed photo from research-grade observations (last resort)

   Birds additionally get a reference to their matching Avicommons photo (`avicommons_image`), which never replaces the selected image.

   eBird placeholder images (no asset ID) are ignored, Wikimedia SVG files (drawings, maps) are skipped, and Wikimedia license names are written in the iNat style (`CC BY-SA 4.0` → `cc-by-sa-4.0`, `Public domain` → `pd`).

**Merge phase:**
Assembles per-species descriptions from multiple sources with the following priority:
- **Base layer — Wikipedia:** English extract plus all locale extracts and Wikipedia URLs
- **LLM overlay:** While `llm.enabled` is true, each locale the LLM provides replaces the description for that locale, except that a translation yields to substantive native Wikipedia text (see Step 11). LLM locales are tracked in the `translate_locales` field

The effective priority is therefore **LLM > Wikipedia**, applied
per-locale, with native Wikipedia text kept over a translation where it is
substantive. With `llm.enabled: false` the build uses only Wikipedia
descriptions. eBird text is never published, so the data can stay under CC BY-SA 4.0.

The build also writes one CSV per model crosswalk; see [model-labels.md](model-labels.md). Output fields, quality filtering, and BirdNET IDs are covered in [metadata.md](metadata.md).

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

LLM text is included only while `llm.enabled` is true; turn it off for a
release that should contain no generated description text.

The `raw_data/`, `dev/`, and `dist/` directories are all gitignored. Zip archives from `dist/` are attached to GitHub releases.

## Step 13 — Images

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

## Project Structure

```
config.py                  # Configuration helpers for config.yml access
config.yml                 # Project settings: taxonomy version, groups, filters, API params
requirements.txt           # Python dependencies
bn_ids.json                # Persistent BirdNET species ID registry (git-tracked)
build/
    metadata.py            # Merge collected sources into final metadata outputs
collectors/
    _common.py             # Shared collector utilities (cache, JSON I/O, shutdown)
    avicommons.py          # Avicommons bird photo index (referenced images)
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
    sound_classes.py       # Reviewed non-species sound classes
dev/                       # Development metadata snapshots and local build artifacts
dist/                      # Published metadata and generated site image assets
docs/                      # Project documentation
overrides/
    bird_splits.csv        # Git-tracked birds kept separate from an AviList lump
    common_name_aliases.csv  # Git-tracked manual common-name search aliases
    model_labels/          # Git-tracked crosswalks from audio model outputs to the taxonomy
    priority_species.csv   # Git-tracked reviewed taxonomy additions
    sound_classes.csv      # Git-tracked reviewed non-species sound classes
    species_aliases.csv    # Git-tracked manual scientific-name aliases
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
    templates/             # Jinja2 templates (home, species, about, download)
```
