# Taxonomy Rules

What the taxonomy includes, and how names, identifiers, descriptions, and images are chosen.

## Taxon Groups

Configured in `config.yml`. Birds include all species; other groups are limited to species with sound observations on iNaturalist.

| Group | iNat Taxon ID | Mode | Min sound observations |
|-------|---------------|------|------------------------|
| Aves | 3 | All species | — |
| Mammalia | 40151 | Sounds only | 1 |
| Insecta | 47158 | Sounds only | 1 |
| Reptilia | 26036 | Sounds only | 1 |
| Amphibia | 20978 | Sounds only | 1 |
| Actinopterygii | 47178 | Priority only | — |
| Chondrichthyes | 196614 | Priority only | — |
| Arachnida | 47119 | Priority only | — |
| Malacostraca | 47187 | Priority only | — |

A group with `priority_only: true` is never bulk-fetched and holds only the species in
`overrides/priority_species.csv`. Species added this way skip the sound observation
threshold, so a group can follow a model's labels.

Mammals can define nested sub-taxa for focused coverage. The current v0.5
configuration includes `Chiroptera` (iNat taxon ID `40268`) under `Mammalia`
to include bat species with at least one sound observation, plus species with
at least 100 total iNaturalist observations.

## Scope and Names

The biological taxonomy is species-level only. Scientific names must be clean
binomial species names: no genera-only rows, higher ranks, subspecies,
trinomials, hybrids, parenthetical qualifiers, slash alternatives, or informal
annotations. Subspecies encountered in source data are folded into their parent
binomial species for now.

Common names are also treated as display names. Parenthetical qualifiers,
bracketed notes, slash alternatives, and informal symbols are rejected in final
metadata. Regular spaces, apostrophes, and dashes/hyphens are allowed when they
are part of the accepted name.

### Birds

- AviList (currently v2026) is the authority for bird taxonomy and English bird
  names. Every iNaturalist bird resolves to its AviList species (by exact name,
  English name within the same genus or epithet stem, eBird code, or as an
  AviList subspecies) and the AviList scientific name becomes canonical.
- iNat taxa that AviList lumps or treats as subspecies (e.g. *Corvus cornix* →
  *Corvus corone*) are folded into that species as `scientific_name_aliases`.
- Reviewed exceptions in `overrides/bird_splits.csv` stay separate species
  (e.g. Hooded Crow *Corvus cornix*): they keep their own name, iNat data, eBird
  code, and English name (looked up by scientific name in the eBird taxonomy).
- Species AviList marks as extinct are excluded; possibly extinct and
  extinct-in-the-wild species are kept.

Non-bird groups are included through configured iNaturalist coverage rules and
reviewed priority species additions.

### iNaturalist taxa

Taxa that iNaturalist has retired (taxon swaps and merges) are dropped; their
names become aliases of the replacement taxon.

### Identifiers

- Every external identifier (eBird, GBIF, NCBI, Avibase, BirdLife, Macaulay
  Library, Xeno-Canto, observation.org, iNat, Wikidata) points to exactly one
  record. An ID reached only through an alias loses to the species whose own
  name carries it; IDs that stay ambiguous (typically after a recent split) are
  dropped. The build fails if a shared ID slips through.
- `gbif_id` is GBIF's accepted key for the species: a synonym key is replaced
  by its accepted key when the accepted name keeps the species' epithet (a
  genus change, or GBIF ranking it as a subspecies) and neither the key nor
  that name belongs to another species here (a GBIF lump keeps the original
  key).
- Identifiers must match their format: numeric GBIF, NCBI, BirdLife, and
  observation.org IDs; 16-hex Avibase IDs; lowercase eBird codes; `Q`-prefixed
  Wikidata QIDs.

### Names

- When two species carry the same localized name, the more authoritative source
  (AviList > eBird > iNat > Wikidata) keeps it; on a tie, the species whose
  Wikipedia article in that language has the name as its title, else the
  more-observed one. The other loses that locale's name.
- Never used as a localized common name (the next source's name is used
  instead): a known scientific name, or (from iNat or Wikidata) an unrecorded
  synonym — a known genus with the species' epithet or a gender variant of it
  ("Leptodactylus andreae" for *Adenomera andreae*), unless the genus word
  heads longer vernacular names in that locale ("Colibri à gorge rubis"); a
  Latin-only name in a locale written in
  another script (an untranslated English fallback, e.g. in `zh`); a multi-word
  English name copied into another locale (eBird `pt_BR` lists untranslated
  species in lower-case English); file names, catalogue numbers, or names
  ending in a disambiguation number.
- When two non-bird species share an English display name on iNat, the one
  whose English Wikipedia lead gives that name keeps it (then one with no other
  English name, then the more-observed one). The others take an unused English
  name from their Wikipedia lead or iNat (spelling variants of the shared name
  don't count), else fall back to the scientific name.
- Non-bird English names get AviList-style capitalisation ("Heart-nosed Bat");
  bird names stay as AviList writes them. The English entry in `common_names`
  is always the display name, and absent when that is the scientific name.

### Descriptions

- Wikipedia descriptions must be about the species. An English article that
  names neither the species nor an alias (iNat sometimes links a sister species,
  the genus, or a clade) is dropped together with its other-language versions
  and any LLM text made from it, unless it is the article of a monotypic genus.
- Any description (Wikipedia in any language, or LLM) whose opening names
  another species of the taxonomy and never this one is dropped as well; the
  LLM translator treats such locales as missing so a translation takes their
  place. Links to rejected articles are dropped with them.
- An English article claimed by several species stays with the species it names
  first.
- LLM texts call the species by its localized common name: translations are
  given the published names, and the build replaces a different opening name
  in older translations ("Der Bolivien-Schlüpfer (*Cranioleuca henricae*)" →
  "Der La-Paz-Baumschlüpfer …"), along with exact repeats of it.
- A stored locale text is used only with that locale's article URL or a
  provenance label from `utils.translate`; unlabelled machine translations left
  by older runs are ignored.
- Wikipedia pages that only announce a split are not descriptions, and hatnotes
  ("The name X can also refer to Y") are removed.
- Texts are cleaned of citation marks, sentences with leftover wiki markup,
  section headings, repeated sentences, and reference-list text (author–year
  citations, ISBNs, URLs, archive-link notes, link lists) that survived
  section selection.

### Images

A photo claimed by several species stays with one of them (the species named in
the file, else the most commonly observed). A photo whose license requires attribution
(anything but public domain or CC0) is only used with a known author, and
Commons files of a skull, skeleton, or postage stamp, or whose file name names
another species of the taxonomy, are skipped; otherwise the next image source
is tried.

### Sound classes

Non-species sound classes live in `overrides/sound_classes.csv`. These records use
their English display name as `scientific_name`, carry `record_type: sound_class`, and
are grouped separately from biological taxa: `Anthropogenic`, `Geophony`, `Human`,
`Music`, and `Soundscape`.
