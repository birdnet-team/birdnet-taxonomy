# Metadata Reference

What the build publishes and how to read it. The steps that produce it are in [pipeline.md](pipeline.md).

## Output Files

The JSON output contains full multilingual descriptions and every collected common-name locale. The CSV output is a lighter export and does not include description excerpts; scientific and common-name aliases and metadata quality flags are exported as pipe-separated fields. It has one `common_name_<locale>` column per locale listed under `csv_export.locales` in `config.yml`, so the column set stays stable between builds.

## Aliases and Description Sources

The JSON output also includes `description_sources`, a per-locale source map,
and `scientific_name_aliases`, a list of clean binomial scientific names that
resolve to the canonical species. Non-species sound classes use
`record_type: sound_class`; their `scientific_name_aliases` and
`common_name_aliases` are both search fallbacks rather than biological names.
Reviewed manual scientific-name bridges live in `overrides/species_aliases.csv`,
and one of these outranks any source that gives the same name to another species.
Species get `common_name_aliases` only from `overrides/common_name_aliases.csv`
(columns `scientific_name`, `alias`, `notes`): reviewed search terms that are
not names, such as `human vocal` for *Homo sapiens*. The build fails on unknown
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
sources cover it. With `metadata_quality.keep_model_labels`, the same holds for
species with status `keep` in a model crosswalk.

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

Downstream consumers should not use `birdnet_id` as a join key across pre-1.0 taxonomy versions; match on `scientific_name`, `scientific_name_aliases`, or an external identifier instead. IDs were last reassigned for v0.5-Oct2026, after mapping the BirdNET v3.0 audio model added 1,557 records (fish, sharks and rays, arachnids, crabs and relatives, more species in other groups, and new sound classes). Bird IDs did not change; 5,510 other IDs did. The ranges above date from v0.4-Oct2026.
