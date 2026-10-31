# Web Server and API

A web UI and REST API for browsing and serving the dataset.

## Running the Server

Browse and search the dataset through a web UI and REST API:

```bash
python -m web.server            # serves dist/species_metadata.json
python -m web.server --dev      # serves dev/species_metadata.json
```

| Flag | Description |
|------|-------------|
| `--host ADDR` | Bind address (default: 127.0.0.1) |
| `--port N` | Bind port (default: 8000) |
| `--dev` | Load metadata from dev/ instead of dist/ (with plain `uvicorn`, set `SPECIES_DATA_DEV=1`) |
| `--workers N` | Worker processes (default: 1; not combinable with `--reload`) |
| `--reload` | Auto-reload on code changes |

The server loads the metadata at startup; restart it after a new build.

Or with hot-reload during development:

```bash
uvicorn web.server:app --reload
```

## Deployment

For sub-path deployment behind a reverse proxy (e.g. `https://example.com/taxonomy/`),
add the URL prefix:

```
ROOT_PATH=/taxonomy
HOST_NAME=https://birdnet.cornell.edu
```

The web app will then generate links under that prefix and accepts deployments where
the reverse proxy either preserves the prefix or strips it before forwarding.

`HOST_NAME` is used for absolute image URLs in built metadata and API responses. With the example above, JSON image URLs and CSV `image_url` values are emitted under `https://birdnet.cornell.edu/taxonomy/api/image/...`.

## Species Lookup

**Species lookup** supports multiple identifier types. The `/species/{name}` and `/api/species/{name}` endpoints accept any of:
- Scientific name (e.g., `Turdus merula`)
- Common name in any locale (e.g., `Amsel`, `Merle noir`)
- BirdNET ID (e.g., `BN10484`)
- Scientific alias from `scientific_name_aliases`
- Common-name alias from `common_name_aliases`
- eBird species code (e.g., `eurbla`)
- Macaulay Library taxon code (e.g., `eurbla`, or `t-…` for non-birds)
- Xeno-Canto name (`xc_name`)
- iNaturalist taxon ID (e.g., `12716`)

HTML species pages redirect to the canonical scientific name URL when accessed via an alias. An unknown name renders a placeholder page (dummy image, a not-found note, and a search link) instead of a 404, like the API.

## Image Proxy

**Image proxy** (`/api/image/{name}?size=thumb|medium`) serves species images with on-demand downloading, smart cropping, and caching. Returns a dummy image (BirdNET logo on gray background) for unknown species or failed downloads. All images cached with 24-hour `Cache-Control` headers.

## Routes

| Route | Description |
|-------|-------------|
| `/` | Home page — search, browse, filter by taxon group |
| `/species/{name}` | Species detail page (HTML) |
| `/about` | About the dataset (HTML) |
| `/download` | Download page (HTML) |
| `/api/image/{name}?size=` | Image proxy — `thumb` (150×100), `medium` (480×320, default) |
| `/api/species` | List species (JSON/CSV) with filtering, sorting, field selection; CSV omits description excerpts |
| `/api/species/{name}` | Single species detail (JSON) with field selection; an unknown name returns a placeholder record (see below) |
| `/api/search?q=` | Search species by name with full query options |
| `/api/suggest?q=` | Autocomplete suggestions (`limit`, `lang`) |
| `/api/locales` | List locale codes present in the dataset |
| `/api/download/csv`, `/json`, `/zip` | Download the release files |
| `/api/fields` | List all available field names |
| `/api/model-labels/{model}` | An audio model's surviving labels and how each maps onto the taxonomy; filter with `status` |
| `/api/groups` | List taxon groups with counts |
| `/api/stats` | Dataset statistics |
| `/docs` | Interactive API docs (Swagger UI) |

## API Query Parameters

All list/search endpoints (`/api/species`, `/api/search`) support these parameters (`/api/search` requires `q`; `/api/species` accepts it as an optional search term):

| Parameter | Example | Description |
|-----------|---------|-------------|
| `fields` | `?fields=scientific_name,common_name` | Return only specified fields (comma-separated) |
| `exclude` | `?exclude=common_names,descriptions` | Return all fields except these |
| `locale` | `?locale=en,de,fr` | Filter `common_names` and `descriptions` to specific locales |
| `sort` | `?sort=-commonness` | Sort by field; prefix `-` for descending |
| `group` | `?group=Aves` | Filter by taxon group |
| `has_image` | `?has_image=true` | Filter species with/without images |
| `has_description` | `?has_description=true` | Filter species with/without English description |
| `description_source` | `?description_source=llm,wikipedia` | Filter by description source |
| `min_commonness` | `?min_commonness=80` | Minimum `commonness` (0–100) |
| `max_commonness` | `?max_commonness=20` | Maximum `commonness` (0–100) |
| `format` | `?format=csv` | Response format — `json` (default) or `csv` |
| `page` | `?page=2` | Page number (default 1) |
| `per_page` | `?per_page=100` | Results per page (1–500, default 50) |

The detail endpoint (`/api/species/{name}`) supports `fields`, `exclude`, and `locale`.

**Unknown names** don't break clients: by default `/api/species/{name}` answers an unknown name with HTTP 200 and a placeholder record — `found: false`, the requested name as `scientific_name` and `common_name`, `record_type: unknown`, empty `common_names`/`descriptions`, and `image.thumb`/`image.medium` URLs that serve the dummy image (the image proxy does the same for unknown names). `found` is kept even when `fields` leaves it out, and is absent on real records. Pass `fallback=false` to get a 404 instead.

## Examples

```bash
# Top 10 most commonly observed birds with images
curl '/api/species?group=Aves&has_image=true&sort=-commonness&per_page=10&fields=scientific_name,common_name,commonness'

# German and French names/descriptions for a species
curl '/api/species/Anas%20platyrhynchos?locale=de,fr&fields=scientific_name,common_names,descriptions'

# Export all mammals as CSV
curl '/api/species?group=Mammalia&per_page=500&format=csv' > mammals.csv

# Search with field selection
curl '/api/search?q=eagle&fields=scientific_name,common_name&per_page=20'

# Look up a species by eBird code
curl '/api/species/eurbla'

# Strict lookup: 404 instead of a placeholder for an unknown name
curl '/api/species/Foo%20bar?fallback=false'
```
