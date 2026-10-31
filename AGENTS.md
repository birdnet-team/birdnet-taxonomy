# AGENTS.md

Guidance for AI coding agents working in this repository. Read this before making
changes so your work matches the project's conventions.

## What this project is

A data pipeline that collects, merges, and serves species metadata for **BirdNET+
Taxonomy**. It covers birds, mammals, insects, reptiles, amphibians, fish, spiders,
and crustaceans, plus reviewed non-species sound classes (e.g. rain, engines). A chain of
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
  name. The only non-species records are the sound classes listed in
  `overrides/sound_classes.csv` (`record_type: sound_class`).

## Pipeline order

Collectors depend on earlier output, so order matters:

1. AviList → 2. iNaturalist → 3. eBird → 4. Wikidata → 5. Wikipedia →
6. Macaulay Library → 7. Xeno-Canto → 8. observation.org → 9. Avicommons →
10. Sound Classes → 11. LLM Translation (optional) → 12. Build → 13. Images (optional)

Steps 1–2 establish taxonomy; 3–8 enrich and cross-reference; 9 downloads the
Avicommons bird photo index; 10 collects the reviewed sound classes; 11
optionally fills description gaps via LLM (Gemini/OpenAI/Anthropic); 12 merges
everything (and writes `raw_data/taxonomy.json`, which later collector runs use);
13 generates images for the built species list.

## Taxon groups

- Active groups are defined in `config.yml` under `taxon_groups`: **Aves, Mammalia,
  Insecta, Reptilia, Amphibia, Actinopterygii, Chondrichthyes, Arachnida,
  Malacostraca**. Birds include all
  species (AviList authority); other groups are limited to species with sound
  observations on iNaturalist above a `min_observations` threshold. Groups marked
  `priority_only` hold only the species in `overrides/priority_species.csv`.
- Disabled groups are kept as comments in `config.yml`; their cached data is retained
  but excluded from builds via the `allowed_groups` filter in `build_taxonomy()`.
- **AviList is the authority** for bird taxonomy and English common names. The
  Clements/eBird name is kept as `common_name_alt`. iNat taxa AviList lumps are
  folded into the AviList species as aliases, except reviewed exceptions in the
  git-tracked `overrides/bird_splits.csv` (e.g. Hooded Crow), which stay separate.
- When a species has no common name, fall back to the scientific name.

## Identifiers and stability

- Each species has a **BirdNET ID** in the form `BN{5 digits}`, stored in the
  git-tracked `bn_ids.json` registry. IDs fall into ranges set in `config.yml`
  (`birdnet_ids`): birds `BN00001`–`BN19999`, other animals `BN20000`–`BN89999`,
  sound classes `BN90000`–`BN99999`; each range is alphabetical by scientific
  name as of the last reassignment. Before the 1.0 release, BirdNET IDs may be
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
  crop. `gbif_id` is an optional replacement for a wrong GBIF key. Build validation
  is strict and fails on duplicate rows, partial image overrides, invalid anchors,
  or unknown species.

## Model labels

- `overrides/model_labels/<model>.csv` maps each audio model output onto the taxonomy.
  Base species names never change to fit a label; model names become aliases.
- `redirect_to` names the label another should merge into, and generates a one-way
  alias. Set it only if the target is a surviving label with training samples.
- Keep the taxonomy lean. Duplicate, sketchy, or useless labels stay out, with status
  `remove` and a `reason`. The exported CSV tells the model developer what to prune and
  why, and which labels to rename. That detail never goes on taxonomy records, in the
  release zip, or in the API.

## Images

- Generated as WebP in two sizes (thumb, medium) with stable, human-readable
  filenames: `<scientific name>_<common name>_<author>.webp`.
- Freshness is tracked in per-image `.state` JSON sidecars, which keep filenames
  stable while still forcing regeneration when a source URL or crop anchor changes.
- Smart cropping uses a YOLO ONNX model with center-crop fallback. Override anchors
  take precedence over smart crop.
- Birds also reference their matching [Avicommons](https://avicommons.org) photo in
  `avicommons_image` (URLs, author, license). It is matched by `ebird_code` and kept
  only when Avicommons names the same species (scientific name or alias, or English
  name). It is a reference, never the species image: do not proxy, crop, or fall
  back to it.

## Deployment awareness

- The web app supports sub-path hosting via `ROOT_PATH` and absolute image URLs via
  `HOST_NAME` (both from `.env`). Template/site links are root-path-relative; image
  URLs are absolute. Keep species and image URLs URL-encoded.

## Repository hygiene

- `raw_data/`, `dev/`, and `dist/` are gitignored build/cache artifacts. `bn_ids.json`
  and the files in `overrides/` (`species_overrides.csv`, `species_aliases.csv`,
  `common_name_aliases.csv`, `bird_splits.csv`, `priority_species.csv`,
  `sound_classes.csv`, `model_labels/`) are git-tracked and meaningful.
- Secrets (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XC_API_KEY`, etc.)
  live in `.env`, never in `config.yml` or source.
  `utils/llm.py` auto-detects which LLM key is present and uses it.

## Version bumps

`taxonomy.version` in `config.yml` is the source of the version, and the website reads it
from there. A bump must update every hardcoded mention in the same change, then grep for
the old version to confirm none is left:

- `README.md`: the current version
- `LICENSE-DATA.md`: the credit example
- `docs/taxonomy.md`, `docs/pipeline.md`, `docs/metadata.md`: the current version, and the
  last ID reassignment if IDs changed

## Licensing

- Code is MIT (`LICENSE`). The CSV and JSON data are CC BY-SA 4.0 (`LICENSE-DATA.md`).
  Images are excluded and keep their own licenses.
- Published text must be compatible with CC BY-SA 4.0. eBird descriptions are not, so
  they are never published. Check a new text source's license before using it.

## Documentation rule

When you change environment variables, metadata fields, API behavior, image handling,
CLI flags, or contributor workflow, update the matching file in **`docs/`** (and
`CONTRIBUTING.md` where relevant) in the same change. Keep the docs consistent with the
code. `README.md` stays lean, with the overview and quick start only; detail and
reasoning go in `docs/`:

- `docs/pipeline.md`: collectors, flags, build, images, project layout
- `docs/taxonomy.md`: taxon groups and selection rules
- `docs/metadata.md`: output fields, quality filter, BirdNET IDs
- `docs/model-labels.md`: audio model crosswalks
- `docs/web-api.md`: web server, deployment, API

## Working style

- Make focused, reviewable changes. Avoid broad repo-wide rewrites, unrelated
  refactors, or scope creep beyond the task.
- Do not add docstrings, comments, type hints, or error handling to code you did not
  otherwise touch.
- Verify changes: rebuild metadata (`python -m build.metadata`) when the build is
  affected, and check the relevant collector or web route runs cleanly.
- Do not commit or push unless explicitly asked.
