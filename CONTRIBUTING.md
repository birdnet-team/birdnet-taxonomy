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
3. If image behavior changed, run `python -m collectors.images` or test the image proxy directly.
4. Verify the resulting files in `dist/` or `dev/`.
5. Update documentation when behavior or contributor workflow changes.

## Manual Overrides

Community-maintained manual overrides live in `overrides/species_overrides.csv` and are applied during `python -m build.metadata`.

Rules:

- `scientific_name` is required and must match a known species exactly.
- If any of `image_url`, `image_author`, `image_license`, or `image_source` is set, all four are required.
- `image_crop_anchor` is optional and must be an integer from `1` to `9`.
- `source_url` and `notes` are optional but useful for review.

The build fails on invalid anchors, duplicate rows, partial image overrides, or unknown species names.

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