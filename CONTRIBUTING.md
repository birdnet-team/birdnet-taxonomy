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

If you work on Claude translation features, add your API key to `.env`:

```dotenv
ANTHROPIC_API_KEY=...
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

Final taxonomy records are species-level only. Do not add genera, higher ranks,
subspecies, trinomials, hybrids, or informal taxa. Fold subspecies into their
parent binomial species for now.

Scientific and common names must be canonical display names. Do not use
parenthetical qualifiers, bracketed notes, slash alternatives, or informal
symbols. Regular spaces, apostrophes, and dashes/hyphens are fine when they are
part of the accepted name.

## Manual Overrides

Community-maintained manual overrides live in `overrides/species_overrides.csv` and are applied during `python -m build.metadata`.

Rules:

- `scientific_name` is required and must match a known species exactly.
- If any of `image_url`, `image_author`, `image_license`, or `image_source` is set, all four are required.
- `image_crop_anchor` is optional and must be an integer from `1` to `9`.
- `source_url` and `notes` are optional but useful for review.

The build fails on invalid anchors, duplicate rows, partial image overrides, or unknown species names.

## Priority Species

Reviewed issue-specific taxonomy additions live in `overrides/priority_species.csv` and are fetched during `python -m collectors.inat`.

Use this only for species that should be included even if they need special review outside the normal group thresholds. Prefer an iNaturalist taxon ID whenever possible.

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

## Scientific Aliases

Reviewed manual scientific-name bridges live in `overrides/species_aliases.csv`
and are applied during `python -m build.metadata`.

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

update [README.md](README.md) and this file in the same change.
