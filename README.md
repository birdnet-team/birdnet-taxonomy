<p align="center">
  <img src="birdnet-logo-circle.png" width="250" alt="BirdNET Logo">
</p>

<h1 align="center">BirdNET+ Taxonomy</h1>

<p align="center">
    <a href="https://birdnet.cornell.edu/taxonomy/api/stats"><img alt="Taxonomy version" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fbirdnet.cornell.edu%2Ftaxonomy%2Fapi%2Fstats&query=%24.taxonomy_version&label=taxonomy&color=ff6b00"></a>
    <a href="https://birdnet.cornell.edu/taxonomy/api/stats"><img alt="Species count" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fbirdnet.cornell.edu%2Ftaxonomy%2Fapi%2Fstats&query=%24.total_species&label=species&color=00c853"></a>
    <a href="https://birdnet.cornell.edu/taxonomy/docs"><img alt="API docs" src="https://img.shields.io/badge/API-docs-00a6fb"></a>
    <a href="#license"><img alt="Code license: MIT" src="https://img.shields.io/badge/code%20license-MIT-ff2d55"></a>
    <a href="LICENSE-DATA.md"><img alt="Data license: CC BY-SA 4.0" src="https://img.shields.io/badge/data%20license-CC%20BY--SA%204.0-ff2d55"></a>
</p>

Pipeline for collecting and merging species metadata from multiple sources. Covers birds, mammals, insects, reptiles, amphibians, fish, spiders, crustaceans, and reviewed non-species sound classes.

Current working taxonomy version: `v0.5-Oct2026`.

**Important note: While still in development (i.e., all releases prior to 1.0), species IDs may change. We will freeze species IDs upon official release.**

Download the most recent version here: https://birdnet.cornell.edu/taxonomy/download

## What's in the dataset

- **Birds:** all species, following AviList for taxonomy and English names.
- **Other animals:** species with sound observations on iNaturalist, plus handpicked additions.
- **Sound classes:** non-species sounds such as rain, engines, and speech.

Each record has a BirdNET ID (`BN` + 5 digits), localized common names and descriptions, an image, aliases, and links to external sources such as eBird, iNaturalist, GBIF, Wikidata, and Xeno-Canto.

## Quick start

```bash
git clone https://github.com/birdnet-team/birdnet-taxonomy.git
cd birdnet-taxonomy
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Put API keys in a `.env` file. Xeno-Canto needs its own key, and LLM translation needs one of the others:

```
XC_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

Run the steps in order, since later steps depend on earlier output. Rerunning a step skips species it has already processed. `--limit N` caps new items and `--dry-run` previews a run.

| Step | Command |
|------|---------|
| 1. AviList | `python -m collectors.avilist` |
| 2. iNaturalist | `python -m collectors.inat` |
| 3. eBird | `python -m collectors.ebird` |
| 4. Wikidata | `python -m collectors.wikidata` |
| 5. Wikipedia | `python -m collectors.wikipedia` |
| 6. Macaulay Library | `python -m collectors.macaulay` |
| 7. Xeno-Canto | `python -m collectors.xenocanto` |
| 8. observation.org | `python -m collectors.observationorg` |
| 9. Avicommons | `python -m collectors.avicommons` |
| 10. Sound Classes | `python -m collectors.sound_classes` |
| 11. LLM Translation (optional) | `python -m collectors.translate` |
| 12. Build | `python -m build.metadata` |
| 13. Images (optional) | `python -m collectors.images` |

The build works offline and writes the release files to `dist/`. Then browse them locally:

```bash
python -m web.server
```

## Documentation

| Document | Covers |
|----------|--------|
| [Pipeline](docs/pipeline.md) | Each collector, its flags, the build, image generation, and the project layout |
| [Taxonomy rules](docs/taxonomy.md) | Taxon groups, and how names, identifiers, descriptions, and images are chosen |
| [Metadata reference](docs/metadata.md) | Output files and fields, quality filtering, and BirdNET IDs |
| [Model labels](docs/model-labels.md) | Mapping audio model outputs onto the taxonomy |
| [Web server and API](docs/web-api.md) | Running and deploying the server, routes, and query parameters |
| [Contributing](CONTRIBUTING.md) | Workflow, naming rules, and manual overrides |

See also the [Code of Conduct](CODE_OF_CONDUCT.md).

## Data Sources

- **[iNaturalist](https://www.inaturalist.org)** — Taxonomy, common names, observation counts, and photos. Data licensed under various Creative Commons licenses by individual contributors.
- **[GBIF](https://www.gbif.org)** — Species keys and synonyms from the backbone taxonomy.
- **[Catalogue of Life](https://www.catalogueoflife.org)** — Confirms species that iNaturalist does not list.
- **[eBird](https://ebird.org)** — Images, common names, and species codes from the Cornell Lab of Ornithology.
- **[Wikidata](https://www.wikidata.org)** — External identifiers, common name labels, and images. Available under [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
- **[Wikipedia](https://www.wikipedia.org)** — Species descriptions and localized article links. Available under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- **[AviList](https://www.avilist.org)** — The Global Avian Checklist (v2026). AviList Core Team, 2026. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). doi:[10.2173/avilist.v2026](https://doi.org/10.2173/avilist.v2026).
- **[Macaulay Library](https://www.macaulaylibrary.org)** — Taxon codes for cross-referencing audio and visual media from the Cornell Lab of Ornithology.
- **[Xeno-Canto](https://xeno-canto.org)** — Scientific name mappings for cross-referencing the world's largest shared bird and wildlife sound collection.
- **[Avicommons](https://avicommons.org)** — Hand-picked, cropped bird photos, referenced next to each bird's own image. Compiled by Adam Jackson; each photo carries its own Creative Commons license (mostly non-commercial) and photographer credit.
- **[observation.org](https://observation.org)** — Species IDs for cross-referencing to one of Europe's largest biodiversity recording platforms.
- **[AudioSet ontology](https://research.google.com/audioset/ontology/)** — Sound class hierarchy and descriptions. Licensed under CC BY-SA 4.0 by Google.
- **LLM providers** — Optional translation and shortening of Wikipedia extracts, through [Google Gemini](https://ai.google.dev/), [OpenAI](https://openai.com/), or [Anthropic Claude](https://www.anthropic.com/claude).

## License

The **code** in this repository is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

The **taxonomy data**, meaning `species_metadata.csv` and `species_metadata.json`, is licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). See [LICENSE-DATA.md](LICENSE-DATA.md) for what that covers and how to credit the sources.

Neither license covers the **images**, which the data only links to. Each image has its own license and author (`image_license` and `image_author`), and most restrict reuse: more than 60% are non-commercial, and about 15% are © Macaulay Library.

For birds, [Avicommons](https://avicommons.org) is an alternative image source: more than 90% of birds also list an Avicommons photo in `avicommons_image`. Every one has a Creative Commons license and a credited photographer, and none is all rights reserved or no-derivatives, but about three quarters are still non-commercial.

Check the license of each image before you add it to your codebase or product.

## Funding

Our work in the Cornell K. Lisa Yang Center for Conservation Bioacoustics is made possible by the generosity of K. Lisa Yang to advance innovative conservation technologies to inspire and inform the conservation of wildlife and habitats.

The development of BirdNET is supported by the German Federal Ministry of Research, Technology and Space (FKZ 01|S22072), the German Federal Ministry for the Environment, Climate Action, Nature Conservation and Nuclear Safety (FKZ 67KI31040E), the German Federal Ministry of Economic Affairs and Energy (FKZ 16KN095550), the Deutsche Bundesstiftung Umwelt (project 39263/01) and the European Social Fund.

## Partners

BirdNET is a joint effort of partners from academia and industry.
Without these partnerships, this project would not have been possible.
Thank you!

![Logos of all partners](https://tuc.cloud/index.php/s/KSdWfX5CnSRpRgQ/download/box_logos.png)
