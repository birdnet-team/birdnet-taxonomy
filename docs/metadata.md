# Metadata Reference

What the build publishes and how to read it. The steps that produce it are in [pipeline.md](pipeline.md).

## Output Files

The JSON output contains full multilingual descriptions and every collected common-name locale. The CSV output is a lighter export and does not include description excerpts; scientific and common-name aliases and metadata quality flags are exported as pipe-separated fields. It has one `common_name_<locale>` column per locale listed under `csv_export.locales` in `config.yml`, so the column set stays stable between builds.

## Fields

Every record in the JSON output has these fields. A missing identifier is an empty string,
except `inat_id` and `observationorg_id`, which are integers or `null`.

| Field | Meaning |
|-------|---------|
| `birdnet_id` | `BN` and five digits; see [BirdNET IDs](#birdnet-ids) |
| `scientific_name` | Canonical binomial name; for a sound class, its English display name |
| `common_name` | English display name, or the scientific name when there is none |
| `common_name_alt` | The Clements/eBird English name of a bird, when it differs from `common_name` |
| `taxon_group` | Group such as `Aves`; sound classes use `Anthropogenic`, `Geophony`, `Human`, `Music`, or `Soundscape` |
| `record_type` | `species` or `sound_class` |
| `common_names` | Locale → common name |
| `common_name_aliases`, `scientific_name_aliases` | Search aliases; see [Aliases and Description Sources](#aliases-and-description-sources) |
| `descriptions` | Locale → description |
| `description_source` | Where the English description comes from; if there is none, the first other locale's source. Empty without a description |
| `description_sources` | Locale → source of that description |
| `translate_locales` | Locales whose description is LLM text |
| `wikipedia_urls` | Locale → Wikipedia article |
| `image`, `image_author`, `image_license`, `image_source` | The species image and its credit; see [Image Fields](#image-fields) |
| `image_crop_anchor` | `1`–`9` when a reviewed crop position overrides smart crop, otherwise `null` |
| `avicommons_image` | Reference to a bird's Avicommons photo, or `null` |
| `inat_id`, `ebird_code`, `gbif_id`, `ncbi_id`, `avibase_id`, `birdlife_id`, `ml_taxon_code`, `xc_name`, `observationorg_id`, `wikidata_qid` | External identifiers; see [Identifiers](taxonomy.md#identifiers) |
| `commonness` | See [Commonness](#commonness); `null` for sound classes |
| `metadata_quality_score`, `metadata_quality_flags` | See [Quality Filter](#quality-filter) |
| `low_resource` | See [Low-Resource Records](#low-resource-records) |
| `audioset_id`, `parent` | Sound classes only: the AudioSet ID and the parent class, which can be empty |

Description sources are `wikipedia`, `llm` (LLM translation or shortening of Wikipedia
text), `llm_fallback` (LLM text merged from Wikipedia articles in other languages), and
`audioset` (AudioSet's wording for a sound class). `utils.translate` writes its own label,
such as `Source: Wikipedia, translated by MyMemory`.

The CSV has no `common_names`, `descriptions`, `description_sources`, `translate_locales`,
`wikipedia_urls`, `image_crop_anchor`, `audioset_id`, or `parent`, and flattens the image
objects as described under [Image Fields](#image-fields).

## Aliases and Description Sources

The JSON output also includes `description_sources`, a per-locale source map,
and `scientific_name_aliases`, a list of clean binomial scientific names that
resolve to the canonical species. Non-species sound classes use
`record_type: sound_class`; their `scientific_name_aliases` and
`common_name_aliases` are both search fallbacks rather than biological names.
Reviewed manual scientific-name bridges live in `overrides/species_aliases.csv`,
and one of these outranks any source that gives the same name to another species.
Species get `common_name_aliases` from `overrides/common_name_aliases.csv`
(columns `scientific_name`, `alias`, `notes`): reviewed search terms that are
not names, such as `human vocal` for *Homo sapiens*. `overrides/label_aliases.csv`
(columns `scientific_name`, `alias`, `kind`) adds further reviewed scientific and
common-name aliases. The build fails on unknown
species, invalid or duplicate aliases, or an alias that is another species'
common name.
The build also filters source-derived aliases that collide with another
canonical species and fails if any alias conflict remains.

## Quality Filter

The build also computes `metadata_quality_score` and
`metadata_quality_flags` before BirdNET IDs are assigned. When
`metadata_quality.enabled` is true, species below `metadata_quality.min_score`
(or a stricter per-group score under `metadata_quality.group_min_scores`, e.g.
insects) or matching the configured thin-record rule are dropped and listed in the
configured report CSV, defaulting to `dev/metadata_quality_report.csv`. Groups
under `metadata_quality.keep_groups` (birds: AviList is the authority) are
scored but never dropped, so a new AviList split stays even before other
sources cover it. With `metadata_quality.keep_registered`, the same holds for any
species with a BirdNET ID in `bn_ids.json`.

`metadata_quality_flags` lists what a record lacks:

- `missing_english_description`: no English description
- `short_english_description`: an English description under 40 words
- `few_localized_descriptions`: at most one locale has 40 words or more
- `missing_image`: no real image
- `weak_external_ids`: fewer than two external identifiers
- `missing_common_name`: no common name other than the scientific name

## Low-Resource Records

Every record gets `low_resource`: `true` when it has no real image or no English
description (sound classes count the image only). It is set by the build and
clears itself once the data arrives. A record without a real image has only
`image.thumb` and `image.medium`, with no `image.src`; those URLs serve the dummy
image. The audit reports these records and does not fail on their missing text.

## Commonness

Each species gets `commonness`, a number from 0 to 100 (one decimal): the percentile of
its iNaturalist observation count within its taxon group (100 = the most
observed species of the group; species with equal counts share a value; sound
classes have none). It measures how often a species is observed and recorded
on iNaturalist, not how abundant it is in an area: conspicuous, widespread,
or easily photographed species score high, while abundant but secretive or
remote ones may score low. Comparing within taxon groups keeps groups with very
different recording effort (birds vs. insects) on the same scale. Raw
observation counts are used only inside the build (quality score, tie-breaks)
and are not published. The web UI labels it very common (80+), common (60+),
moderately common (40+), uncommon (20+), or rare (below 20).

## Image Fields

Images are not covered by the project's MIT License. Check each image's license before reuse; see [License](../README.md#license).

Image fields in the final metadata:

- JSON metadata stores an `image` object with `src`, `thumb`, and `medium`
- CSV metadata flattens this to a single `image_url` column containing the local served medium image URL
- The API moves `image_source`, `image_author`, and `image_license` into the `image` object (`source`, `author`, `license`)
- Birds with a matching [Avicommons](pipeline.md#step-9--avicommons) photo also carry `avicommons_image` (`null` otherwise): `src` (900 px), `thumb` (160 px), and `medium` (480 px) URLs on `static.avicommons.org`, plus `author` and `license`. It is a reference only: these URLs are not proxied or cropped, and the species `image` is unchanged
- CSV metadata flattens it to `avicommons_image_url` (the 480 px URL), `avicommons_image_author`, and `avicommons_image_license`

## BirdNET IDs

Each record receives a BirdNET ID in the format `BN{5 digits}`, stored in the git-tracked `bn_ids.json` registry. IDs fall into fixed ranges (`birdnet_ids` in `config.yml`):

| Range | Records | Example |
|-------|---------|---------|
| `BN00001`–`BN19999` | Birds | `BN00483` (Mallard) |
| `BN20000`–`BN89999` | Other animals | `BN21007` (Coyote) |
| `BN90000`–`BN99999` | Non-species sound classes | `BN90000` (Accordion) |

`--reassign-ids` numbers each range alphabetically by scientific name from its start. Normal builds preserve existing IDs and give a new record the next number after the highest ID in its range (IDs of removed records stay reserved), so alphabetical order holds only as of the last reassignment. The build fails if a range is full or a kept ID lies outside its record's range. Before the 1.0 release, IDs may be intentionally reassigned with `--reassign-ids` as a documented breaking change.

Downstream consumers should not use `birdnet_id` as a join key across pre-1.0 taxonomy versions; match on `scientific_name`, `scientific_name_aliases`, or an external identifier instead. IDs were last reassigned for v0.5-Nov2026, after a reviewed batch added 1,557 records (fish, sharks and rays, arachnids, crabs and relatives, more species in other groups, and new sound classes). Bird IDs did not change; 5,510 other IDs did. The ranges above date from v0.4-Oct2026.
