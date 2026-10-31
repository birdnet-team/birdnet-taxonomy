# Contributing

Thanks for contributing to BirdNET+ Taxonomy.

## Ground Rules

Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

Contributions should stay focused on the project goals:

- improving taxonomy coverage or metadata quality
- fixing pipeline bugs or regressions
- improving performance, documentation, or deployment
- adding carefully reviewed manual overrides for specific species

## Development Setup

```bash
git clone https://github.com/birdnet-team/birdnet-taxonomy.git
cd birdnet-taxonomy
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you work on LLM translation features, add one LLM key to `.env` (the provider
is auto-detected in the order Gemini → OpenAI → Anthropic); Xeno-Canto lookups
need `XC_API_KEY`:

```dotenv
GEMINI_API_KEY=...      # or OPENAI_API_KEY / ANTHROPIC_API_KEY
XC_API_KEY=...
```

If you run the web app behind a sub-path, also set:

```dotenv
ROOT_PATH=/taxonomy
HOST_NAME=https://birdnet.cornell.edu
```

## Typical Workflow

1. Update or collect raw source data as needed.
2. Rebuild metadata with `python -m build.metadata` or `python -m build.metadata --merge-only`.
3. If description coverage changed, run `python -m utils.audit_descriptions dist/species_metadata.json --output dev/description_audit.csv` or the same command against `dev/species_metadata.json`.
4. If image behavior changed, run `python -m collectors.images` or test the image proxy directly.
5. Verify the resulting files in `dist/` or `dev/`.
6. Update documentation when behavior or contributor workflow changes.

## Naming Rules

Biological taxonomy records are species-level only. Do not add genera, higher
ranks, subspecies, trinomials, hybrids, or informal taxa. Fold subspecies into
their parent binomial species for now.

AviList is the bird authority: bird records use the AviList scientific name,
and iNat taxa AviList lumps or treats as subspecies are folded into the AviList
species as aliases. When a bird is renamed by an AviList release, update
references to it in `overrides/*.csv` to the new name (the build fails on
unknown names), keeping the old name as an alias where it helps lookups.

After collector changes or new source releases, check the build output for:
`Identifiers shared by several records` (must be 0; the build fails otherwise),
`Xeno-Canto names ... rejected`, and `Localized names re-assigned/dropped`.
Stale shared IDs in cached raw data can be re-resolved with
`python -m collectors.observationorg --recheck-shared`,
`python -m collectors.macaulay --recheck-shared`, and
`python -m collectors.xenocanto --revalidate`.

Non-species sound classes are the only exception. Add them to
`overrides/sound_classes.csv`, not in biological taxonomy or priority-species
overrides. These records use the English display name as `scientific_name` and
must carry `record_type: sound_class` in built metadata.

Scientific and common names must be canonical display names. Do not use
parenthetical qualifiers, bracketed notes, slash alternatives, or informal
symbols. Regular spaces, apostrophes, and dashes/hyphens are fine when they are
part of the accepted name.

## Manual Overrides

Community-maintained manual overrides live in `overrides/species_overrides.csv` and are applied during `python -m build.metadata`. They are tracked in Git and always take precedence over fetched image data. Current support covers image replacement and manual crop anchoring.

Rules:

- `scientific_name` is required and must match a known species exactly.
- If any of `image_url`, `image_author`, `image_license`, or `image_source` is set, all four are required.
- `image_crop_anchor` is optional and must be an integer from `1` to `9`: a 3×3 grid position that bypasses smart crop, where `5` is center crop, `3` is top-right, `7` is bottom-left, etc.
- `gbif_id` is optional and replaces a wrong GBIF key, such as a homonym in another kingdom. Say why in `notes`.
- `source_url` and `notes` are optional but useful for review.

The build fails on invalid anchors, duplicate rows, partial image overrides, or unknown species names.

If an override changes the effective image URL or crop anchor, the image pipeline regenerates the same named `.webp` file in place. Cache freshness is tracked separately in per-image JSON sidecars under `dev/images/*/.state/` or `dist/images/*/.state/`.

## Common-Name Aliases

Reviewed common-name search aliases for species live in
`overrides/common_name_aliases.csv` (`scientific_name`, `alias`, `notes`), one
alias per row. Use them for search terms that are not names but should find a
species, e.g. `human vocal` or `speech` for *Homo sapiens*. Real names belong
in the source data, and scientific synonyms in `overrides/species_aliases.csv`.

The build fails on unknown species, invalid or duplicate aliases, or an alias
that is another species' common name (unless that record redirects to this one
in a model crosswalk).

## Model Labels

`overrides/model_labels/<model>.csv` maps each output of an audio model onto the
taxonomy. [docs/model-labels.md](docs/model-labels.md) describes the columns. Keep the taxonomy lean: a label that is
a duplicate, sketchy, or not useful stays out, with status `remove`, and the export
tells the model developer to prune it.

- Give every `remove` and `review` row a `reason`, so the CSV for the model developer can
  say why. Use `note` for specifics the standard wording misses. Reasons stay out of
  taxonomy records, the release zip and the API.
- Set `redirect_to` only if the target is a surviving label with training samples.
- Set `status` to `pending` when a label's record is planned, and to `keep` once the
  record exists. The build fails if either no longer matches the records.
- Never rename a species to fit a label. Model names become aliases.

## Priority Species

Reviewed issue-specific taxonomy additions live in `overrides/priority_species.csv` and are fetched during `python -m collectors.inat`.

Use this only for species that should be included even if they need special review outside the normal group thresholds. Prefer an iNaturalist taxon ID whenever possible. A species iNaturalist does not list stays in as a record without an iNat taxon, so first confirm it in GBIF or Catalogue of Life and name that source in `source`.

Required columns:

- `scientific_name`
- `taxon_group`
- `source`
- `reason`

Optional columns:

- `inat_id`
- `gbif_id`
- `common_name`

Rules:

- `scientific_name` must be a clean binomial species name.
- Do not add subspecies; fold them into the parent species for now.
- Use the accepted/current source name as the row's `scientific_name`.
- Put old names or issue-specific context in `reason`.
- Keep additions small and reviewed.

## Bird Splits

AviList is the bird authority, so iNat taxa it lumps are folded into the
AviList species. `overrides/bird_splits.csv` lists reviewed exceptions that stay
separate species, for example where users need to tell two widespread forms
apart (Hooded Crow vs Carrion Crow).

Required columns:

- `scientific_name` — the species kept separate (must be an active iNat taxon)
- `avilist_species` — the living AviList species that lumps it

Optional columns: `source`, `reason`.

The build fails if `scientific_name` is itself an AviList species or
`avilist_species` is not a living AviList species. Keep this list short and
justified: every row is a deliberate deviation from AviList.

## Scientific Aliases

Reviewed manual scientific-name bridges live in `overrides/species_aliases.csv`
and are applied during `python -m build.metadata`. A reviewed alias outranks any
source that gives the same name to another species, which fixes a synonym that
followed the wrong half of a split.

Required columns:

- `scientific_name`
- `alias`

Optional columns:

- `source`
- `notes`

Rules:

- Both `scientific_name` and `alias` must be clean binomial species names.
- `scientific_name` must already exist in the taxonomy.
- Do not add subspecies, hybrids, genera, parenthetical notes, slash alternatives, or informal annotations.
- Do not use an alias that is the canonical scientific name of another included species.
- Keep each alias reviewed and source-backed; broad synonym imports belong in collectors, not this file.

## Pull Requests

AI-assisted contributions are welcome. If you use AI tools, keep the output focused on a single issue, feature, or cleanup rather than submitting large mixed changes or oversized commits.

Prefer small, reviewable diffs over broad repository-wide rewrites.

When opening a change, include:

- what changed
- why it changed
- how you verified it
- whether metadata, docs, or generated assets were rebuilt

Small, focused pull requests are much easier to review than broad mixed changes.

## Documentation

If you add or change:

- environment variables
- metadata fields
- API behavior
- image handling
- contributor workflow

update the matching file in [docs/](docs/), and this file, in the same change. Keep
[README.md](README.md) lean: it holds the overview and quick start, and detail and
reasoning belong in `docs/`.
