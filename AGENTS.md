# AGENTS.md

Guidance for AI coding agents working in this repository. Read this before making
changes so your work matches the project's conventions.

## What this project is

A data pipeline that collects, merges, and serves species metadata for **BirdNET+
Taxonomy**. It covers birds, mammals, insects, reptiles, and amphibians. A chain of
**collectors** fetch from external sources into `raw_data/`, a **build** step merges
everything offline into final metadata, and a **FastAPI web server** browses and
serves the dataset (HTML UI, REST API, image proxy).

## Core principles

- **Configuration lives in `config.yml`.** Locales, taxon groups, rate limits,
  worker counts, batch sizes, model names, and API endpoints belong there — not
  hardcoded in scripts. Access it through the helpers in `config.py`.
- **CLI flags override config, never duplicate it.** When a collector exposes a
  tunable that also lives in `config.yml` (e.g. `--workers`, `--rps`,
  `--save-every`), use a sentinel default (`0`) on the argparse argument and fall
  back to the config value when the flag is unset. Do not bake the real default
  into the CLI.
- **Collectors are incremental and idempotent.** Rerunning a collector skips
  already-processed species. Preserve this: support `--limit N` (cap new items),
  `--dry-run` (preview without fetching), and `--new-only` where it applies.
  Save progress periodically so long runs survive interruption.
- **The build step is purely offline.** `build/metadata.py` must not make network
  calls. It only merges data already present in `raw_data/`.
- **Species only, with canonical names.** The pipeline tracks binomial species,
  not genera, higher ranks, subspecies, or hybrids. Subspecies are folded into
  their top-level species for now. Use the shared binomial-name filter in
  `collectors/_common.py`; do not reinvent name validation. Final scientific and
  common names must be canonical display names only: no parenthetical qualifiers,
  bracketed notes, slashes, symbols, or informal annotations. Regular spaces,
  apostrophes, and dashes/hyphens are fine when they are part of the accepted
  name.

## Pipeline order

Collectors depend on earlier output, so order matters:

1. AviList → 2. iNaturalist → 3. eBird → 4. Wikidata → 5. Wikipedia →
6. Macaulay Library → 7. Xeno-Canto → 8. observation.org → 9. Claude (optional) →
10. Images (optional) → 11. Build

Steps 1–2 establish taxonomy; 3–8 enrich and cross-reference; 9 fills description
gaps with Claude; 10 generates images; 11 merges everything.

## Taxon groups

- Active groups are defined in `config.yml` under `taxon_groups`: **Aves, Mammalia,
  Insecta, Reptilia, Amphibia**. Birds include all species (AviList authority);
  other groups are limited to species with sound observations on iNaturalist above a
  `min_observations` threshold.
- Disabled groups are kept as comments in `config.yml`; their cached data is retained
  but excluded from builds via the `allowed_groups` filter in `build_taxonomy()`.
- **AviList is the authority** for bird taxonomy and English common names. The
  Clements/eBird name is kept as `common_name_alt`.
- When a species has no common name, fall back to the scientific name.

## Identifiers and stability

- Each species has a **BirdNET ID** in the form `BN{5 digits}`, stored in the
  git-tracked `bn_ids.json` registry. Before the 1.0 release, BirdNET IDs may be
  reassigned as an intentional breaking change when taxonomy cleanup requires it.
  Reassignment must be explicit (`--reassign-ids`), documented in the same
  change, and never done accidentally. After 1.0, treat IDs as permanent unless
  project maintainers explicitly change that policy.

## Manual overrides

- Community overrides live in the git-tracked `overrides/species_overrides.csv` and
  are applied during the build.
- `scientific_name` must match a known species exactly.
- If any image field (`image_url`, `image_author`, `image_license`, `image_source`)
  is set, **all four are required**.
- `image_crop_anchor` is an optional `1`–`9` 3×3 grid position that bypasses smart
  crop. Build validation is strict and fails on duplicate rows, partial image
  overrides, invalid anchors, or unknown species.

## Images

- Generated as WebP in two sizes (thumb, medium) with stable, human-readable
  filenames: `<scientific name>_<common name>_<author>.webp`.
- Freshness is tracked in per-image `.state` JSON sidecars, which keep filenames
  stable while still forcing regeneration when a source URL or crop anchor changes.
- Smart cropping uses a YOLO ONNX model with center-crop fallback. Override anchors
  take precedence over smart crop.

## Deployment awareness

- The web app supports sub-path hosting via `ROOT_PATH` and absolute image URLs via
  `HOST_NAME` (both from `.env`). Template/site links are root-path-relative; image
  URLs are absolute. Keep species and image URLs URL-encoded.

## Repository hygiene

- `raw_data/`, `dev/`, and `dist/` are gitignored build/cache artifacts. `bn_ids.json`
  and `overrides/species_overrides.csv` are git-tracked and meaningful.
- Secrets (`ANTHROPIC_API_KEY`, `XC_API_KEY`, etc.) live in `.env`, never in
  `config.yml` or source.

## Documentation rule

When you change environment variables, metadata fields, API behavior, image handling,
CLI flags, or contributor workflow, update **`README.md`** (and `CONTRIBUTING.md` where
relevant) in the same change. Keep the docs consistent with the code.

## Working style

- Make focused, reviewable changes. Avoid broad repo-wide rewrites, unrelated
  refactors, or scope creep beyond the task.
- Do not add docstrings, comments, type hints, or error handling to code you did not
  otherwise touch.
- Verify changes: rebuild metadata (`python -m build.metadata`) when the build is
  affected, and check the relevant collector or web route runs cleanly.
- Do not commit or push unless explicitly asked.
